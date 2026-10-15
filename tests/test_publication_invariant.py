"""Regression lock: retrieved evidence implies a published answer, never canned copy.

The invariant under test
------------------------
When retrieval returned chunks AND the project scope resolved AND evidence
integrity is intact, the published result MUST be an answer. It must never be one
of the two canned copies (``LOW_GROUNDING_MESSAGE`` /
``VERIFICATION_FALLBACK_MESSAGE``). Cases 4 to 6 and 9 pin the opposite direction
so a fix cannot open the valve too far: with no usable evidence, a foreign scope,
or a chunk that failed scope validation, the canned copy is still the correct
output.

Layer under test, and why this one
----------------------------------
The defect is a PUBLICATION decision, so these tests drive the real
``RagQueryWorkflow.output_guard`` step, which is the only place that decides
whether the draft or a canned copy is released. The step is invoked directly with
a real ``llama_index`` ``Context`` seeded from the store; the workflow's own
``_store_get`` contract documents direct invocation with a partial store as
supported, and ``tests/test_workflow_stamp.py`` already drives steps that way.

Two weaker layers were rejected deliberately. Asserting on ``guard_output`` alone
cannot observe the publish decision at all -- the same verdict can publish or be
discarded depending on scope, integrity and evidence state. Re-deriving
``publish_original`` inside this file would assert a copy of the product's boolean
expression instead of the product's behaviour, so it would keep passing after the
real gate regressed. The harness therefore supplies INPUTS (chunks, scope,
registry rows, draft) and lets production code decide.

Evidence is never hand-written. ``merged.meta["_chunk_numeric_evidence"]`` is
derived by the product itself from the real chunk text through the verifier's own
extractor, so a broken extractor turns the chunk-authority cases red instead of
being papered over by a hand-built list of Decimals.

Offline by construction: the registry pool, the audit writer and the prompt
template's project-identity read are replaced. No network, no database.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from decimal import Decimal

import pytest

from api import get_cfg
import api.application.pipelines.workflow as workflow_module
import api.application.services.merge as merge_module
import api.application.services.project_config as project_config_module
from api.application.pipelines.workflow import (
    FALLBACK_REASONS,
    LOW_GROUNDING_MESSAGE,
    VERIFICATION_FALLBACK_MESSAGE,
    GeneratedEv,
    RagQueryWorkflow,
)
from api.application.services.generate import build_messages
from api.application.services.merge import merge_context
from api.application.services.sql_leg import build_fact_evidence
from api.domain.services.guard_output import (
    _CURRENCY_UNITS,
    _chunk_binding_evidence,
    _extract_amount_matches,
    _is_money_claim,
)
from api.domain.services.rewrite import RoutedResult
from llama_index.core.workflow import Context

# The two canned copies are imported, never copied: a copy would keep passing
# after the product's wording changed, which is exactly when the lock matters.
CANNED_COPIES = (LOW_GROUNDING_MESSAGE, VERIFICATION_FALLBACK_MESSAGE)

AS_OF = date(2026, 10, 7)
DOC_ID = "project-camellia-2026q3"
TITLE = "Hồ sơ tổng quan dự án The Camellia Sơn Trà - Đà Nẵng"
CHUNK_ID = "project-camellia-2026q3:3:3"
EFFECTIVE_FROM = date(2026, 8, 13)

# The real retrieved chunk (same document as the investigation probe). Its text
# states six amenity areas, so an answer that quotes them is faithful to a
# retrieved source while carrying no SQL fact at all.
AMENITY_CONTENT = (
    "42 tiện ích đa tầng: Tầng 1 (sảnh chuẩn khách sạn, shophouse, sân chơi trẻ em, "
    "The Forest Heart), Tầng 2 (nhà trẻ, thư viện, Business Lounge), Tầng 3 (Gym, "
    "Yoga, Event Ballroom, Kid Club, bể bơi trong nhà), Tầng mái Sky Park (6 vườn "
    "chủ đề, view 360 độ Sơn Trà)\n- Diện tích tiện ích: nha_tre 434.2 m2, shcd_tang2 "
    "157.4 m2, be_boi 381.6 m2, gym 134.3 m2, yoga 43.6 m2, shcd_tang3 232.1 m2"
)
AMENITY_ANSWER = (
    "Dự án The Camellia có 42 tiện ích đa tầng.\n"
    "Diện tích tiện ích:\n- nha_tre 434.2 m2\n- shcd_tang2 157.4 m2\n"
    "- be_boi 381.6 m2\n- gym 134.3 m2\n- yoga 43.6 m2\n- shcd_tang3 232.1 m2"
)
AMENITY_FIGURES = ("434.2", "157.4", "381.6", "134.3", "43.6", "232.1")

# Chunk prose that states a MONEY figure. Money must stay fact-only: a chunk that
# merely mentions a price is not evidence of that price.
MONEY_CONTENT = (
    "Bảng giá tham khảo The Camellia Sơn Trà: căn CH-03 giá từ 2.850.000.000 đồng, "
    "đã bao gồm VAT."
)
MONEY_ANSWER = "Căn CH-03 tại The Camellia có giá 2.850.000.000 đồng theo hồ sơ dự án."
MONEY_VALUE = Decimal("2850000000")

# A descriptive question: no price cue and no unit code, so the publication gate
# takes the evidence path rather than the price-limitation path. Those paths carry
# different copies, and mixing them would make a red case ambiguous.
QUERY = "Dự án The Camellia có những tiện ích gì và diện tích từng khu là bao nhiêu?"

AMENITY_SECTION = "Tiện ích"
# A non-amenity section is what disables the pre-existing, narrow amenity-area
# exception (``merge._rag_area_evidence`` returns nothing unless the registry
# section is exactly "tiện ích"). It is how these tests separate the NEW
# chunk-text authority from the old label-pinned one.
NON_AMENITY_SECTION = "Tổng quan"

SCOPE = "camellia"
FOREIGN_SCOPE = "soleil"

# --- Round-2 cases: C1 (money), C2 (subject binding), C3 (reason honesty) -------
# A chunk that binds TWO different unit types to two different areas. The whole
# point of the binding cases is that presence of a figure in the chunk is not
# authority for the subject the answer names, so the numbers must exist in the
# chunk while belonging to another subject.
UNIT_TYPE_CONTENT = "căn Studio diện tích 32.5 m2, căn 2PN diện tích 68.4 m2"
# One subject, two legitimate areas (net vs. wall-to-wall). The veto has to
# aggregate per subject, or a faithful quote of EITHER value is refused.
MULTI_VALUE_CONTENT = "CH-01: 68.3 m2 thong thuy / 73.8 m2 tim tuong"
# Neither subject is a unit code, a unit type, a unit kind or a named amenity, so
# neither is bindable -- see the deliberate fail-open case below.
UNBINDABLE_CONTENT = "sàn hầm 800 m2, khu cây xanh 500 m2"

# --- Round-3 cases: every case runs through BOTH veto branches ---------------
# The veto has two entry branches and they failed on DIFFERENT case sets, so a case
# that exercises only one of them leaves half the defect unguarded. Measured on the
# pre-T10 snapshot: with ``area_evidence`` populated the amenity-label branch vetoed
# the spaced labels correctly but ALSO refused a faithful comma-separated list; with
# ``area_evidence`` empty the chunk-binding branch let the spaced labels ship AND
# still refused the list. Every case below therefore runs in both configurations.
AMENITY_LIST_CONTENT = (
    "Diện tích tiện ích: nha_tre 434.2 m2, shcd_tang2 157.4 m2, be_boi 381.6 m2, "
    "gym 134.3 m2, yoga 43.6 m2, shcd_tang3 232.1 m2"
)
UNIT_LIST_CONTENT = "Bảng hàng: studio 32.5 m2, 2pn 68.4 m2"
UNIT_CODES_CONTENT = "Bảng hàng: CH-03 diện tích 68.3 m2, CH-04 diện tích 70.1 m2."
BALCONY_CONTENT = "Bảng hàng: CH-03 diện tích 68.3 m2 thông thủy, ban công 5.2 m2."
MULTI_CLAUSE_CONTENT = "Bảng hàng: CH-01 diện tích 68.3 m2 thông thủy / 73.8 m2 tim tường."
# Binds ch-01 to 68.3 while stating 70.0 for ch-04, so the presence authority alone
# would allow a 70.0 answer for ch-01 and only the binding can refuse it.
VETO_CONTROL_CONTENT = "Bảng hàng: CH-01 diện tích 68.3 m2, CH-04 diện tích 70.0 m2."

# A faithful quote of all six amenity areas, comma separated. This is the shape that
# regressed: the whole list sits in ONE clause, because a comma does not end a clause.
AMENITY_LIST_ANSWER = (
    "Diện tích tiện ích: nha_tre 434,2 m², shcd_tang2 157,4 m², be_boi 381,6 m², "
    "gym 134,3 m², yoga 43,6 m², shcd_tang3 232,1 m²"
)
# The four multi-word amenity labels written with SPACES, each misattributed to a
# figure the chunk states for a DIFFERENT amenity. The underscore spelling matched
# the binding token and hid the defect; the spaced spelling is how a model actually
# writes Vietnamese. Each entry is (label the draft names, figure it steals, draft).
SPACED_AMENITY_ANSWERS = (
    ("nha_tre", Decimal("381.6"), "Nhà trẻ tại The Camellia rộng 381,6 m²."),
    ("be_boi", Decimal("434.2"), "Bể bơi tại The Camellia rộng 434,2 m²."),
    ("shcd_tang3", Decimal("434.2"), "Sinh hoạt cộng đồng tầng 3 tại The Camellia rộng 434,2 m²."),
    ("shcd_tang2", Decimal("434.2"), "Sinh hoạt cộng đồng tầng 2 tại The Camellia rộng 434,2 m²."),
)
AREA_EVIDENCE_CONFIGS = (
    ("area_populated", AMENITY_SECTION),
    ("area_empty", NON_AMENITY_SECTION),
)

MONEY_FIGURE = "2.850.000.000"
# Every spelling that must stay blocked with no fact behind it. The first two are
# the ones that reached customers; the rest pin the group against a regression.
MONEY_SPELLINGS = (
    ("dong", f"Căn CH-03 tại The Camellia có giá {MONEY_FIGURE} đồng."),
    ("ty", "Căn CH-03 tại The Camellia có giá 2,85 tỷ."),
    ("ti", "Căn CH-03 tại The Camellia có giá 2,85 tỉ."),
    ("trieu", "Căn CH-03 tại The Camellia có giá 2.850 triệu."),
    ("bare", "Căn CH-03 tại The Camellia có giá 2850000000."),
)

# A far-past interval keeps the fact row current whatever day the suite runs,
# while still being earlier than the fixed AS_OF the prompt-integrity check uses.
FACT_START = "2020-01-01"
FACT_DOC = "price-camellia-2026q3"
FACT_CAMPAIGN = "camellia-2026q3"

# --- Round-4 part A: a percentage is authorized by the validated chunk --------
# One policy per line, because a comma does not end a clause: a one-line payment
# schedule classifies as a deposit clause for every figure in it, so the
# misattribution case below would pass for the wrong reason.
PCT_CONTENT = (
    "Chinh sach thanh toan The Camellia.\n"
    "Dat coc 5% gia ban khi ky thoa thuan.\n"
    "Lai suat 7,5% moi nam cho phan vay ngan hang.\n"
    "Thanh toan 95% khi ban giao can ho."
)
# The same policy chunk carrying the amenity list as well. It exists so that the
# two ``area_evidence`` configurations are not the same code path run twice: the
# registry-backed amenity evidence only materialises for the amenity section, so
# this variant is what actually puts a percentage claim in front of both veto
# branches. Measured: the percentage evidence is identical across the two variants
# (``_chunk_pct_evidence`` keeps only ``%`` units and every amenity figure is m2),
# so any verdict difference between the variants is the amenity branch leaking into
# percentage authority -- which is the defect this pair of variants exists to catch.
PCT_AMENITY_CONTENT = PCT_CONTENT + "\n" + AMENITY_LIST_CONTENT
PCT_MONEY_LINE = "Gia ban 3.000.000.000 dong cho can ho tieu chuan."
PCT_MONEY_CONTENT = PCT_CONTENT + "\n" + PCT_MONEY_LINE
PCT_MONEY_AMENITY_CONTENT = PCT_MONEY_CONTENT + "\n" + AMENITY_LIST_CONTENT
# Both variants that carry the amenity list, i.e. the ones whose registry evidence
# materialises in the amenity section. Kept as a named set so the branch assertion
# below states the rule once instead of listing chunk names at each call site.
PCT_AMENITY_BEARING = (PCT_AMENITY_CONTENT, PCT_MONEY_AMENITY_CONTENT)
PCT_CHUNK_VARIANTS = (
    ("pct_only", PCT_CONTENT),
    ("pct_plus_amenity", PCT_AMENITY_CONTENT),
)
PCT_MONEY_CHUNK_VARIANTS = (
    ("pct_only", PCT_MONEY_CONTENT),
    ("pct_plus_amenity", PCT_MONEY_AMENITY_CONTENT),
)

PCT_DEPOSIT_FAITHFUL = "Dat coc 5% gia ban khi ky thoa thuan."
# 88% is stated by no retrieved chunk, so no authority in the gate can license it.
# The counterweight that keeps the chunk authority from becoming "any percentage
# is fine", and it is why this case sits next to the publish case below.
PCT_DEPOSIT_FABRICATED = "Dat coc 88% gia ban khi ky thoa thuan."
PCT_RATE_FAITHFUL = "Lai suat 7,5% moi nam cho phan vay ngan hang."
# 5% IS stated by the chunk, but the chunk states it for the DEPOSIT policy. The
# figure is real and the commitment is not, so presence alone cannot see this --
# only comparing the answer's own cue against the chunk's cue can.
PCT_RATE_MISATTRIBUTED = "Lai suat 5% moi nam cho phan vay ngan hang."
# Money that the chunk text states, with no fact row behind it. Money is fact-only,
# so the chunk authority must refuse it even though the value is right there.
PCT_MONEY_ANSWER = "Can ho tieu chuan co gia 3.000.000.000 dong."
PCT_MONEY_VALUE = Decimal("3000000000")


def _money_fact_evidence(value: int = 2_850_000_000) -> list[dict]:
    """FACT_EVIDENCE for one unit's price, built by production's own producer.

    Shaped like ``build_sql``'s projection and passed through the real
    ``build_fact_evidence``, because ``_sql_confirms_fact`` re-reads the leg rows
    and ``_has_current_financial_lineage`` re-checks every lineage column. A
    hand-built evidence dict would not prove the positive control can be reached.
    """
    row = {
        "fact_id": 1,
        "subject_key": f"unit:{SCOPE}/CH-03",
        "policy_key": "htls",
        "campaign_key": FACT_CAMPAIGN,
        "fact_key": "price_vnd",
        "value_num": value,
        "value_text": None,
        "unit": "vnd",
        "quality": "exact",
        "range_min": None,
        "range_max": None,
        "effective_from": FACT_START,
        "effective_to": None,
        "source_doc_id": FACT_DOC,
        "source_chunk_id": None,
        "trust_level": "confirmed",
        "source_doc_status": "published",
        "source_doc_effective_from": FACT_START,
        "source_doc_effective_to": None,
        "source_doc_project_key": SCOPE,
        "campaign_project_key": SCOPE,
        "campaign_status": "active",
        "campaign_effective_from": FACT_START,
        "campaign_effective_to": None,
        "campaign_source_doc_id": FACT_DOC,
        "campaign_source_doc_status": "published",
        "campaign_source_doc_effective_from": FACT_START,
        "campaign_source_doc_effective_to": None,
        "campaign_source_doc_project_key": SCOPE,
    }
    return build_fact_evidence([row], "facts", date.today())


def _registry_row(content: str, section: str, project_key: str) -> dict:
    """One row shaped like ``merge.hydrate_chunks``'s registry SELECT."""
    return {
        "chunk_id": CHUNK_ID,
        "registry_content": content,
        "doc_id": DOC_ID,
        "title": TITLE,
        "section": section,
        "kind": "project",
        "project_key": project_key,
        "status": "published",
        "effective_from": EFFECTIVE_FROM,
        "effective_to": None,
    }


class _Connection:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetch(self, _sql: str, ids) -> list[dict]:
        return [row for row in self._rows if row["chunk_id"] in ids]

    async def fetchrow(self, *_args, **_kwargs):
        return None


class _Pool:
    """Stand-in for the read-only registry pool: a chunk set, no database."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def acquire(self):
        rows = self._rows

        class _Acquire:
            async def __aenter__(self_inner):
                return _Connection(rows)

            async def __aexit__(self_inner, *_args):
                return None

        return _Acquire()


def _strong_threshold() -> float:
    """The live strong-chunk bar, read from the setting the ``merge`` step reads.

    The bar belongs to the active rerank model, so it is configuration rather than
    a property of this pipeline. Restating it as a literal here would let these
    cases keep describing a scenario production can no longer reach the moment the
    setting moves -- and case 2 is the customer's own failure, so it has to stay
    faithful to the live bar.
    """
    return float(get_cfg("rerank_strong_threshold", 0.5))


def _strong_chunks(raw_chunks: list[dict], selected_ids) -> int:
    """Mirror the ``merge`` step's strong-chunk count.

    Replicated because these tests enter the pipeline at ``output_guard`` and so
    never run ``merge``. The expression and the threshold both come from
    production's own source, so the ``strong_chunks`` these cases assert on means
    the same thing it means live.
    """
    threshold = _strong_threshold()
    return sum(
        1
        for chunk_id in (selected_ids or ())
        if float(next((c for c in raw_chunks if c.get("id") == chunk_id), {}).get("score", 0.0))
        >= threshold
    )


def _routed() -> RoutedResult:
    return RoutedResult(
        rewritten=QUERY,
        routing={
            "needs_rag": True,
            "needs_sql": False,
            "structured_path": "none",
            "needs_geo": False,
        },
        sql_spec=None,
        hl_keywords=[],
        ll_keywords=[],
        high_stakes=False,
        as_of=None,
    )


def _run_publication(
    monkeypatch,
    *,
    answer: str,
    chunk_content: str = AMENITY_CONTENT,
    section: str = AMENITY_SECTION,
    score: float = 0.91,
    scope: str | None = SCOPE,
    record_project: str = SCOPE,
    retrieval_project: str | None = SCOPE,
    retrieved: bool = True,
    guard_raises: bool = False,
    facts: list[dict] | None = None,
    visible_limit: int | None = None,
) -> tuple[dict, object, dict]:
    """Drive the real publication gate once; return (result, merged, audit).

    Only INPUTS are injected. The draft, the retrieved chunk, the registry row, the
    resolved scope and the selected fact rows are handed to production code, and the
    returned result is whatever the real ``output_guard`` step decided to release.
    """
    facts = list(facts or [])

    async def _fake_pool():
        return _Pool([_registry_row(chunk_content, section, record_project)])

    monkeypatch.setattr(merge_module, "get_ro_pool", _fake_pool)

    # The audit writer persists to Postgres; the publication decision does not
    # depend on it, and the test must stay offline.
    async def _recording_audit(_entry: dict) -> None:
        return None

    monkeypatch.setattr(workflow_module, "write_audit", _recording_audit)

    # The prompt template renders project identity through a best-effort DB read
    # with a connect timeout. Left real, it turns every case into a connection
    # attempt; the identity text is irrelevant to the publication decision.
    monkeypatch.setattr(project_config_module, "fetch_project_identity", lambda _key: {})

    if guard_raises:

        async def _exploding_guard(*_args, **_kwargs):
            raise RuntimeError("verifier unavailable")

        monkeypatch.setattr(workflow_module, "guard_output", _exploding_guard)

    raw_chunks = [{"id": CHUNK_ID, "content": chunk_content, "score": score}] if retrieved else []

    # Real hydration + real prompt serialization: these are what publication
    # authority is decided against, so faking them would test nothing.
    merged = asyncio.run(
        merge_context(QUERY, raw_chunks, facts, AS_OF, scope if raw_chunks else None)
    )
    merged.meta.update(
        query=QUERY,
        rewritten=QUERY,
        as_of=AS_OF.isoformat(),
        project_key=scope,
        retrieval_project_key=retrieval_project,
        degraded=[],
        sql_row_count=len(facts),
        strong_chunks=_strong_chunks(raw_chunks, merged.meta.get("selected_chunk_ids")),
        has_approx=False,
        geo_places=0,
        answer_mode="normal",
    )
    # The merge step keeps the leg rows beside the prompt projection, and
    # ``_sql_confirms_fact`` re-reads them to prove a selected row really came from
    # the leg. Passing the same objects is what the real merge step does.
    merged.meta["_fact_evidence"] = facts
    build_messages(merged, [])

    async def _go() -> tuple[dict, dict, object]:
        wf = RagQueryWorkflow()
        ctx = Context(workflow=wf)
        seeded = {
            "merged": merged,
            "answer": answer,
            "routed": _routed(),
            "sql_result": None,
            "visible_limit": visible_limit,
            "audit": {},
            "t0": time.perf_counter(),
            "degraded": [],
            "trace_id": "t-invariant",
            "presentation_images": [],
            "presentation_facts": [],
            "geo_result": None,
            "as_of_date": AS_OF,
        }
        for key, value in seeded.items():
            await ctx.store.set(key, value)
        done = await wf.output_guard(ctx, GeneratedEv())
        # The store does not hand the step the same object it was given, so the
        # instance the step enriched -- and the evidence it derived onto it -- is
        # only observable by reading the key back. Asserting on the local object
        # instead would silently see the pre-run state.
        return done.result, await ctx.store.get("audit"), await ctx.store.get("merged")

    result, audit, released_merged = asyncio.run(_go())
    # The finalization block is the only producer of trace_id in the result, so
    # this proves the gate ran through to its release point instead of bailing out
    # earlier.
    assert result["trace_id"] == "t-invariant"
    return result, released_merged, audit


def _chunk_evidence(merged) -> list:
    """The product's own chunk-derived figures, or a clear failure if absent.

    Read after the run because production derives it inside ``output_guard``. A
    missing key means the chunk-text authority was removed, which the money-guard
    and chunk-authority cases must not silently tolerate.
    """
    evidence = merged.meta.get("_chunk_numeric_evidence")
    assert isinstance(evidence, list), (
        "production must publish the chunk-derived numeric evidence on merged.meta"
    )
    return evidence


def _chunk_binding(merged, chunk_content: str) -> list:
    """The subject bindings the gate used, checked against the real extractor.

    Read from the enriched ``merged`` because ``output_guard`` derives them there,
    then compared with ``_chunk_binding_evidence`` run over the same chunk text.
    An earlier independent harness hand-built ``meta`` with no bindings at all, so
    its "the wrong subject still ships" result described the harness rather than
    production; comparing against the real extractor is what keeps that from
    happening here, and it also fails loudly if the producer stops running.
    """
    binding = merged.meta.get("_chunk_binding_evidence")
    assert isinstance(binding, list), (
        "production must publish the chunk-derived subject bindings on merged.meta"
    )
    assert binding == _chunk_binding_evidence([{"content": chunk_content}]), (
        "the gate's bindings must be the ones the real extractor reads from this chunk"
    )
    return binding


def _assert_veto_branch(merged, chunk_content: str, section: str) -> list:
    """Assert which veto branch this run reached, and return its amenity evidence.

    The registry-backed evidence only materialises for the amenity section, and it
    is what makes the guard's amenity-label branch run instead of the chunk-binding
    branch. Asserting the entry count keeps each case honest about which branch it
    exercised, so a case cannot quietly run the same branch twice and leave the
    other one unguarded -- which is how the round-2 defect survived a 27-case suite.
    """
    area = merged.meta.get("_rag_area_evidence")
    assert isinstance(area, list), "production must publish the registry amenity evidence"
    expected = 6 if (section == AMENITY_SECTION and chunk_content == AMENITY_LIST_CONTENT) else 0
    assert len(area) == expected, (
        f"this case must reach the {expected}-entry branch, got {len(area)} entries"
    )
    return area


def _assert_pct_branch(merged, chunk_content: str, section: str) -> list:
    """Assert which veto branch a percentage case reached, and return its evidence.

    ``_assert_veto_branch`` keys its expectation on one named chunk, and the
    percentage cases deliberately run the SAME claim on two chunks -- one carrying
    amenity labels and one not -- so that the two ``area_evidence`` configurations
    exercise different code paths instead of the same one twice. Without the
    amenity-carrying variant the payment chunk yields no registry evidence at all
    and the second configuration would silently repeat the first.
    """
    area = merged.meta.get("_rag_area_evidence")
    assert isinstance(area, list), "production must publish the registry amenity evidence"
    expected = 6 if (section == AMENITY_SECTION and chunk_content in PCT_AMENITY_BEARING) else 0
    assert len(area) == expected, (
        f"this case must reach the {expected}-entry branch, got {len(area)} entries"
    )
    return area


def _pct_evidence(merged) -> list:
    """The product's own percentage evidence, or a clear failure if absent.

    Read after the run because production derives it inside ``output_guard``. A
    missing key means the chunk-text authority was removed, which the percentage
    cases must not silently tolerate.
    """
    evidence = merged.meta.get("_chunk_pct_evidence")
    assert isinstance(evidence, list), (
        "production must publish the chunk-derived percentage evidence on merged.meta"
    )
    return evidence


def _assert_t18_branch(merged, chunk_content: str, section: str) -> list:
    """Assert which veto branch a range-binding case reached, and return its evidence.

    ``_assert_veto_branch`` keys its expectation on the one named amenity chunk, and the
    Soleil chunk is a SECOND chunk that carries amenity labels: it yields three registry
    entries (be_boi, gym, yoga) in the amenity section rather than none. Asserting the
    real count keeps the two ``area_evidence`` configurations from silently becoming the
    same code path run twice, which is the property that helper exists to protect.
    """
    area = merged.meta.get("_rag_area_evidence")
    assert isinstance(area, list), "production must publish the registry amenity evidence"
    expected = 3 if section == AMENITY_SECTION else 0
    assert len(area) == expected, (
        f"this case must reach the {expected}-entry branch, got {len(area)} entries"
    )
    return area


# ---------------------------------------------------------------------------
# 1. Chunks present, scope valid, integrity intact: the answer MUST ship
# ---------------------------------------------------------------------------


def test_1_valid_chunk_with_resolved_scope_publishes_the_answer(monkeypatch):
    """A scope-valid chunk plus a faithful draft publishes the draft itself."""
    result, merged, _audit = _run_publication(monkeypatch, answer=AMENITY_ANSWER)

    assert result["answer"] not in CANNED_COPIES
    assert result["answer"] == AMENITY_ANSWER
    # The evidence really reached the gate, so a pass here is not vacuous.
    assert merged.meta["selected_chunk_ids"] == (CHUNK_ID,)
    assert merged.meta["strong_chunks"] == 1
    assert [s["doc_id"] for s in result["sources"]] == [DOC_ID]


def test_2_chunk_below_rerank_threshold_still_publishes(monkeypatch):
    """A chunk under the rerank threshold is weak evidence, not absent evidence.

    ``strong_chunks`` feeds the confidence tier only. This is the customer's turn:
    retrieval returned the chunk, the scope resolved, and the draft quoted it
    correctly, yet the published result was a canned copy.
    """
    # Scored against the live bar rather than a fixed number, so this case keeps
    # describing a genuine threshold miss however the setting moves.
    result, merged, _audit = _run_publication(
        monkeypatch, answer=AMENITY_ANSWER, score=_strong_threshold() / 2
    )

    # Non-vacuous: the rerank bar really was missed, and the chunk really was
    # still the selected evidence.
    assert merged.meta["strong_chunks"] == 0
    assert merged.meta["selected_chunk_ids"] == (CHUNK_ID,)

    assert result["answer"] not in CANNED_COPIES
    assert result["answer"] == AMENITY_ANSWER


def test_3_verifier_exception_with_usable_chunk_publishes_with_review(monkeypatch):
    """A verifier outage must not delete an answer retrieval already grounded.

    The draft keeps its evidence rights, and the turn is flagged for review
    instead: refusing a correct, sourced answer is the data loss this locks out.
    """
    result, merged, _audit = _run_publication(
        monkeypatch, answer=AMENITY_ANSWER, guard_raises=True
    )

    assert result["answer"] not in CANNED_COPIES
    assert result["answer"] == AMENITY_ANSWER
    assert result["requires_review"] is True
    assert merged.meta["selected_chunk_ids"] == (CHUNK_ID,)


# ---------------------------------------------------------------------------
# 2. The other direction: with no usable evidence the canned copy is CORRECT
# ---------------------------------------------------------------------------


def test_4_no_chunk_and_no_fact_may_ship_the_canned_copy(monkeypatch):
    """Nothing retrieved and nothing in the facts: there is nothing to publish."""
    result, merged, _audit = _run_publication(
        monkeypatch, answer=AMENITY_ANSWER, retrieved=False
    )

    assert merged.meta["selected_chunk_ids"] == ()
    assert result["answer"] in CANNED_COPIES
    assert result["sources"] == []


def test_5_unresolved_scope_may_ship_the_canned_copy(monkeypatch):
    """A chunk whose project scope never resolved authorizes nothing.

    The chunk WAS retrieved -- only the scope is missing -- so this pins that
    retrieval alone is not publication authority.
    """
    result, merged, _audit = _run_publication(
        monkeypatch,
        answer=AMENITY_ANSWER,
        scope=None,
        retrieval_project=None,
    )

    assert merged.meta["selected_chunk_ids"] == ()
    assert result["answer"] in CANNED_COPIES


def test_6_chunk_from_another_project_may_ship_the_canned_copy(monkeypatch):
    """Evidence belonging to another project must not answer this project."""
    result, merged, _audit = _run_publication(
        monkeypatch,
        answer=AMENITY_ANSWER,
        scope=FOREIGN_SCOPE,
        record_project=SCOPE,
        retrieval_project=FOREIGN_SCOPE,
    )

    assert merged.meta["selected_chunk_ids"] == ()
    assert result["answer"] in CANNED_COPIES
    assert result["sources"] == []


# ---------------------------------------------------------------------------
# 3. Chunk text as numeric authority for NON-money figures (the regression)
# ---------------------------------------------------------------------------


def test_7_chunk_text_authorizes_non_money_figures(monkeypatch):
    """Figures quoted faithfully from a validated chunk are not orphans.

    The registry section is not an amenity section, so the narrow label-pinned
    amenity exception cannot fire at all -- the draft's areas have no authority
    except the chunk text itself. Before the chunk-text authority existed, the
    verifier reported exactly these six areas as orphans and the gate replaced a
    correct, sourced answer with the canned copy.
    """
    result, merged, audit = _run_publication(
        monkeypatch, answer=AMENITY_ANSWER, section=NON_AMENITY_SECTION
    )

    # Non-vacuous, two ways: the old amenity path was unavailable, and the
    # product's own extractor really did read the areas out of the chunk text.
    assert merged.meta.get("_rag_area_evidence") == []
    evidence = _chunk_evidence(merged)
    for figure in AMENITY_FIGURES:
        assert Decimal(figure) in evidence, figure

    assert result["answer"] not in CANNED_COPIES
    assert result["answer"] == AMENITY_ANSWER
    verdicts = (audit.get("guard_verdicts") or {})
    assert verdicts.get("numeric_grounding") == "pass"


def test_8_money_quoted_from_chunk_prose_is_still_blocked(monkeypatch):
    """Money is never authorized by chunk prose, only by a fact.

    This is the over-correction guard: the same digits that make a non-money
    figure publishable appear here inside the chunk text AND inside the chunk
    numeric evidence, and the answer must STILL be refused. Exact sale prices
    exist for only a few units, so a chunk that merely mentions a price is not
    evidence of it.
    """
    result, merged, _audit = _run_publication(
        monkeypatch,
        answer=MONEY_ANSWER,
        chunk_content=MONEY_CONTENT,
        section=NON_AMENITY_SECTION,
    )

    # Non-vacuous: the chunk really does state the figure, and the product really
    # did collect it as chunk-derived evidence.
    assert "2.850.000.000" in MONEY_CONTENT
    assert MONEY_VALUE in _chunk_evidence(merged)

    assert result["answer"] in CANNED_COPIES
    assert MONEY_ANSWER not in result["answer"]
    assert result["sources"] == []


def test_9_foreign_scope_empties_the_chunk_numeric_authority(monkeypatch):
    """The chunk-text authority is gated by scope validation, not by the digits.

    Two runs over the SAME chunk text and the SAME draft, differing only in the
    resolved scope. The in-scope run publishes; the foreign-scope run may ship the
    canned copy, because a chunk set that failed validation must authorize
    nothing. Comparing the pair is what proves the gate keys on scope rather than
    on the figures happening to appear in some chunk.
    """
    in_scope, merged_in_scope, _audit_a = _run_publication(
        monkeypatch, answer=AMENITY_ANSWER
    )
    assert in_scope["answer"] == AMENITY_ANSWER
    assert in_scope["answer"] not in CANNED_COPIES
    assert Decimal("434.2") in _chunk_evidence(merged_in_scope)

    foreign, merged_foreign, _audit_b = _run_publication(
        monkeypatch,
        answer=AMENITY_ANSWER,
        scope=FOREIGN_SCOPE,
        record_project=SCOPE,
        retrieval_project=FOREIGN_SCOPE,
    )
    assert merged_foreign.meta.get("selected_chunk_ids") == ()
    assert foreign["answer"] in CANNED_COPIES
    assert foreign["answer"] != in_scope["answer"]


# ---------------------------------------------------------------------------
# 4. C1 — money reaches a customer only through a registry fact, in EVERY
#    spelling. ``đ`` and ``vnđ`` used to normalize to None, so the figure was
#    dropped before the money rule could refuse it and an unbacked price shipped.
# ---------------------------------------------------------------------------


def test_c1_1_money_written_with_d_stays_blocked_without_a_fact(monkeypatch):
    """A price written ``đ`` must not ship when no fact backs it."""
    draft = f"Căn CH-03 tại The Camellia có giá {MONEY_FIGURE} đ."
    result, merged, audit = _run_publication(monkeypatch, answer=draft)

    # Non-vacuous: the chunk really was selected evidence for this turn.
    assert merged.meta["selected_chunk_ids"] == (CHUNK_ID,)
    assert result["answer"] in CANNED_COPIES
    assert result["answer"] != draft
    assert audit.get("fallback_reason") == "numeric_ungrounded"


def test_c1_2_money_written_with_vnd_stays_blocked_without_a_fact(monkeypatch):
    """The ``vnđ`` spelling of the same price is refused the same way."""
    draft = "Căn CH-03 tại The Camellia có giá 2850000000 vnđ."
    result, _merged, audit = _run_publication(monkeypatch, answer=draft)

    assert result["answer"] in CANNED_COPIES
    assert result["answer"] != draft
    assert audit.get("fallback_reason") == "numeric_ungrounded"


@pytest.mark.parametrize(("label", "draft"), MONEY_SPELLINGS, ids=[c[0] for c in MONEY_SPELLINGS])
def test_c1_3_every_other_money_spelling_stays_blocked_without_a_fact(
    monkeypatch, label, draft
):
    """The rest of the money group stays blocked, so C1 cannot regress back."""
    result, _merged, audit = _run_publication(monkeypatch, answer=draft)

    assert result["answer"] in CANNED_COPIES, label
    assert result["answer"] != draft, label
    assert audit.get("fallback_reason") == "numeric_ungrounded", label


def test_c1_4_money_publishes_when_a_registry_fact_binds_the_value(monkeypatch):
    """The counterweight: with a fact behind the price, the answer must ship.

    Without this the C1 fix could be satisfied by making money permanently
    unpublishable, which is the opposite failure: the fact path is the ONLY
    authority money is allowed to have, so it has to keep working.
    """
    draft = f"Căn CH-03 tại The Camellia có giá {MONEY_FIGURE} đồng theo bảng giá."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, facts=_money_fact_evidence()
    )

    assert merged.meta["sql_row_count"] == 1
    assert result["answer"] == draft
    assert result["answer"] not in CANNED_COPIES
    assert audit.get("fallback_reason") is None


# ---------------------------------------------------------------------------
# 5. C2 — a figure the chunk states for ANOTHER subject must not be published
#    under this one. Presence in a chunk is not attribution to a subject.
# ---------------------------------------------------------------------------


def test_c2_5_area_bound_to_a_different_unit_type_is_blocked(monkeypatch):
    """``2PN`` at the Studio's area is a false statement, not a sourced quote."""
    draft = "Căn 2PN tại The Camellia có diện tích 32,5 m²."
    result, merged, audit = _run_publication(
        monkeypatch,
        answer=draft,
        chunk_content=UNIT_TYPE_CONTENT,
        section=NON_AMENITY_SECTION,
    )

    # Non-vacuous: the chunk binds both unit types, and 32.5 really is in the
    # chunk's own figures — so only the binding can refuse this.
    binding = _chunk_binding(merged, UNIT_TYPE_CONTENT)
    assert (Decimal("32.5"), "studio") in binding
    assert (Decimal("68.4"), "2pn") in binding
    assert Decimal("32.5") in _chunk_evidence(merged)

    assert result["answer"] in CANNED_COPIES
    assert result["answer"] != draft
    assert audit.get("fallback_reason") == "numeric_ungrounded"


def test_c2_6_the_unit_types_own_area_publishes(monkeypatch):
    """The faithful quote of the same chunk still ships."""
    draft = "Căn 2PN tại The Camellia có diện tích 68,4 m²."
    result, merged, audit = _run_publication(
        monkeypatch,
        answer=draft,
        chunk_content=UNIT_TYPE_CONTENT,
        section=NON_AMENITY_SECTION,
    )

    assert _chunk_binding(merged, UNIT_TYPE_CONTENT)
    assert result["answer"] == draft
    assert result["answer"] not in CANNED_COPIES
    assert audit.get("fallback_reason") is None


def test_c2_7_amenity_area_bound_to_another_value_is_blocked(monkeypatch):
    """The amenity veto must hold even where the registry table is empty.

    The section is not an amenity section, so ``_rag_area_evidence`` is empty and
    the registry-backed veto cannot fire; the chunk's own binding is the only
    authority that can refuse the nursery's area under ``gym``.
    """
    draft = "Gym tại The Camellia rộng 434,2 m²."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, section=NON_AMENITY_SECTION
    )

    assert merged.meta.get("_rag_area_evidence") == []
    assert (Decimal("134.3"), "gym") in _chunk_binding(merged, AMENITY_CONTENT)
    assert Decimal("434.2") in _chunk_evidence(merged)

    assert result["answer"] in CANNED_COPIES
    assert result["answer"] != draft
    assert audit.get("fallback_reason") == "numeric_ungrounded"


def test_c2_8_the_amenities_own_area_publishes(monkeypatch):
    """A labelled area the chunk agrees with must not become unpublishable.

    This guards the invariant from the other side: the binding must veto a
    CONTRADICTION, not the mere presence of a label.
    """
    draft = "Diện tích nha_tre tại The Camellia là 434,2 m²."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, section=NON_AMENITY_SECTION
    )

    assert (Decimal("434.2"), "nha_tre") in _chunk_binding(merged, AMENITY_CONTENT)
    assert result["answer"] == draft
    assert result["answer"] not in CANNED_COPIES
    assert audit.get("fallback_reason") is None


def test_c2_9_a_label_the_chunk_never_binds_still_publishes(monkeypatch):
    """A subject the chunk never prices contradicts nothing, so presence stands."""
    draft = "Bể bơi tại The Camellia rộng 32,5 m²."
    result, merged, audit = _run_publication(
        monkeypatch,
        answer=draft,
        chunk_content=UNIT_TYPE_CONTENT,
        section=NON_AMENITY_SECTION,
    )

    binding = _chunk_binding(merged, UNIT_TYPE_CONTENT)
    assert all(label != "be_boi" for _value, label in binding)

    assert result["answer"] == draft
    assert result["answer"] not in CANNED_COPIES
    assert audit.get("fallback_reason") is None


def test_c2_13_subject_outside_the_bindable_shapes_fails_open(monkeypatch):
    """A DELIBERATE fail-open, recorded so it stays a decision and not a gap.

    The chunk parser binds a subject by SHAPE -- unit code, unit type, unit kind,
    or one of the six named amenities -- because the positional rule it replaced
    bound junk on the real corpus: measured over 336 chunks, "the token before the
    number" produced 48 pairs of which 24 were the filler "thuy" (from "thông
    thủy"), plus "chuan", "ho", "dong" and "m2". Those junk subjects would veto
    any answer that merely mentioned those words, so the shape rule was chosen:
    62 bound pairs, 0 junk labels, 0 of 62 faithful quotes false-vetoed, and 31 of
    31 real subjects still vetoed when given a foreign value.

    The cost is exactly this case. "sàn hầm" is not one of the four shapes, so the
    chunk binds nothing for it, nothing contradicts, and a figure the chunk states
    for a DIFFERENT subject publishes under it. That is accepted on purpose:
    a missing veto withholds nothing, while a wrong veto replaced a correct answer
    with canned copy -- the customer-visible bug this whole file exists for.
    """
    draft = "Sàn hầm có diện tích 500 m²."
    result, merged, audit = _run_publication(
        monkeypatch,
        answer=draft,
        chunk_content=UNBINDABLE_CONTENT,
        section=NON_AMENITY_SECTION,
    )

    # Non-vacuous: the chunk does state 500 m², just not for this subject, and the
    # parser really did refuse to bind either subject.
    assert Decimal("500") in _chunk_evidence(merged)
    assert _chunk_binding(merged, UNBINDABLE_CONTENT) == []

    assert result["answer"] == draft
    assert result["answer"] not in CANNED_COPIES
    assert audit.get("fallback_reason") is None


def test_c2_14_one_subject_with_two_valid_areas_publishes_both(monkeypatch):
    """One subject may legitimately carry several areas; both quotes must ship.

    The veto aggregates per SUBJECT and only fires when the figure is outside that
    subject's own set of values. Deciding per binding pair instead vetoed 54 of 62
    faithful quotes on the real corpus, because a net area and a wall-to-wall area
    for the same unit read as a contradiction to a per-pair rule.
    """
    first = "Căn CH-01 tại The Camellia có diện tích 68,3 m²."
    second = "Căn CH-01 tại The Camellia có diện tích 73,8 m²."

    result_a, merged_a, audit_a = _run_publication(
        monkeypatch,
        answer=first,
        chunk_content=MULTI_VALUE_CONTENT,
        section=NON_AMENITY_SECTION,
    )
    binding = _chunk_binding(merged_a, MULTI_VALUE_CONTENT)
    assert (Decimal("68.3"), "ch-01") in binding
    assert (Decimal("73.8"), "ch-01") in binding

    result_b, merged_b, audit_b = _run_publication(
        monkeypatch,
        answer=second,
        chunk_content=MULTI_VALUE_CONTENT,
        section=NON_AMENITY_SECTION,
    )
    assert _chunk_binding(merged_b, MULTI_VALUE_CONTENT) == binding

    for result, audit, draft in ((result_a, audit_a, first), (result_b, audit_b, second)):
        assert result["answer"] == draft
        assert result["answer"] not in CANNED_COPIES
        assert audit.get("fallback_reason") is None


# ---------------------------------------------------------------------------
# 6. C3 — ``fallback_reason`` must name the cause that actually fired. Two
#    different failures used to be reported under the visible-bound token, so a
#    grounding failure could hide behind an event that never happened.
# ---------------------------------------------------------------------------


def test_c3_10_sanitizer_created_figure_reports_final_recheck_ungrounded(monkeypatch):
    """No visible bound was applied, so the bound token must not be reported.

    The draft carries no extracted figure: ``32,5`` sits before an internal marker
    and has no unit, so it is invisible to the first check. Stripping the marker
    joins it to ``m2`` and creates a figure the chunk never states -- a final
    recheck failure, not a truncation.
    """
    draft = "Diện tích khu vực 32,5 [chunk_x] m2 theo hồ sơ dự án The Camellia."
    result, _merged, audit = _run_publication(monkeypatch, answer=draft)

    assert result["answer"] in CANNED_COPIES
    assert result["answer"] != draft
    assert audit.get("fallback_reason") == "final_recheck_ungrounded"
    assert audit.get("fallback_reason") != "visible_bound_dropped_qualification"


def test_c3_11_real_visible_bound_reports_visible_bound_dropped_qualification(
    monkeypatch,
):
    """A bound that really clipped a qualified answer reports its own token."""
    draft = (
        f"Căn CH-03 tại The Camellia có giá {MONEY_FIGURE} đồng theo bảng giá. "
        + "Giá đã bao gồm VAT và phí quản lý, liên hệ chủ đầu tư để được tư vấn. " * 60
    )
    result, _merged, audit = _run_publication(
        monkeypatch,
        answer=draft,
        facts=_money_fact_evidence(),
        visible_limit=2000,
    )

    # Non-vacuous: the bound really did shorten this answer.
    assert len(draft) > 2000
    assert result["answer"] in CANNED_COPIES
    assert audit.get("fallback_reason") == "visible_bound_dropped_qualification"


def test_c3_12_every_observed_reason_is_a_declared_fallback_reason(monkeypatch):
    """Whatever reason a turn reports must be one the product declares.

    Collected across the shapes this file can reach, so the set is not a single
    convenient value: a draft that published (None), the money and binding refusals,
    the no-evidence refusal, and both C3 causes.
    """
    observed: set[str] = set()
    turns = (
        {"answer": AMENITY_ANSWER},
        {"answer": f"Căn CH-03 tại The Camellia có giá {MONEY_FIGURE} đ."},
        {"answer": "Căn 2PN tại The Camellia có diện tích 32,5 m².",
         "chunk_content": UNIT_TYPE_CONTENT, "section": NON_AMENITY_SECTION},
        {"answer": AMENITY_ANSWER, "retrieved": False},
        {"answer": "Diện tích khu vực 32,5 [chunk_x] m2 theo hồ sơ dự án The Camellia."},
        {"answer": (
            f"Căn CH-03 tại The Camellia có giá {MONEY_FIGURE} đồng theo bảng giá. "
            + "Giá đã bao gồm VAT và phí quản lý, liên hệ chủ đầu tư để được tư vấn. " * 60
         ), "facts": _money_fact_evidence(), "visible_limit": 2000},
    )
    for turn in turns:
        _result, _merged, audit = _run_publication(monkeypatch, **turn)
        reason = audit.get("fallback_reason")
        if reason is not None:
            observed.add(reason)

    # The declared set is imported, never restated: a copy would keep passing
    # after the product renamed a token.
    assert observed, "at least one refusal must be observable"
    assert observed <= set(FALLBACK_REASONS), sorted(observed - set(FALLBACK_REASONS))
    assert "visible_bound_dropped_qualification" in observed
    assert "final_recheck_ungrounded" in observed


# ---------------------------------------------------------------------------
# 4. Round 3: both veto branches, and both directions, locked together
#
# The round-2 suite missed the defect in BOTH directions because it only ever
# reached ONE of the veto's two branches. With ``area_evidence`` populated the
# guard's amenity-label branch runs first; with it empty the chunk-binding branch
# runs instead. Measured against the pre-T10 snapshot, the two branches failed on
# disjoint case sets: the populated branch refused a faithful comma-separated list
# (over-veto -- a regression against round 1, the answer is right and the bot says
# it cannot answer), while the empty branch published a wrong subject/value pair
# (the section 5 fail-open). Every case below therefore runs in both
# configurations, and each asserts which branch it reached, so no case can silently
# duplicate the other one and leave half the defect unguarded.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_c2_15_gym_with_another_amenitys_area_is_vetoed_in_both_branches(
    monkeypatch, config, section
):
    """The wrong-subject case the amenity branch existed for must survive the merge.

    ``gym`` is stated at 134.3 in the chunk and 434.2 belongs to ``nha_tre``. Both
    branches must refuse it: with amenity evidence present this is that branch's
    original purpose, and with it empty the chunk-binding branch has to carry the
    same verdict, or merging the two branches traded this veto away.
    """
    draft = "Gym tại The Camellia rộng 434,2 m²."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=AMENITY_LIST_CONTENT, section=section
    )
    _assert_veto_branch(merged, AMENITY_LIST_CONTENT, section)
    binding = _chunk_binding(merged, AMENITY_LIST_CONTENT)
    assert (Decimal("134.3"), "gym") in binding
    assert (Decimal("434.2"), "nha_tre") in binding
    # Non-vacuous: 434.2 IS stated in the chunk, so a presence-only gate would
    # publish this draft and only the subject binding can refuse it.
    assert Decimal("434.2") in _chunk_evidence(merged)

    assert result["answer"] in CANNED_COPIES, config
    assert result["answer"] != draft, config
    assert audit.get("fallback_reason") == "numeric_ungrounded", config


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_c2_16_unit_type_area_belongs_to_another_unit_type_and_is_vetoed_in_both_branches(
    monkeypatch, config, section
):
    """The same defect one layer down: 32.5 belongs to ``studio``, not to ``2pn``.

    The subject sits several words away from the figure, so a positional rule that
    reads the token before the number would pick the wrong subject here.
    """
    draft = "Căn 2PN tại The Camellia có diện tích 32,5 m²."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=UNIT_LIST_CONTENT, section=section
    )
    _assert_veto_branch(merged, UNIT_LIST_CONTENT, section)
    binding = _chunk_binding(merged, UNIT_LIST_CONTENT)
    assert (Decimal("32.5"), "studio") in binding
    assert (Decimal("68.4"), "2pn") in binding
    assert Decimal("32.5") in _chunk_evidence(merged)

    assert result["answer"] in CANNED_COPIES, config
    assert result["answer"] != draft, config
    assert audit.get("fallback_reason") == "numeric_ungrounded", config


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
@pytest.mark.parametrize(
    ("named_label", "stolen", "draft"),
    SPACED_AMENITY_ANSWERS,
    ids=[label for label, _stolen, _draft in SPACED_AMENITY_ANSWERS],
)
def test_c2_17_spaced_multiword_amenity_label_is_vetoed_in_both_branches(
    monkeypatch, config, section, named_label, stolen, draft
):
    """The section 5 escape: a multi-word label written with spaces must be refused.

    The chunk spells these labels with underscores (``nha_tre``) while a model writes
    them the way Vietnamese is written (``Nhà trẻ``). The round-2 suite used ``Gym``
    -- one word -- plus ``nha_tre`` with the underscore, i.e. only the two spellings
    that already worked, so the whole multi-word family went untested. On the pre-T10
    snapshot every case here published a wrong figure once the amenity evidence was
    empty.
    """
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=AMENITY_LIST_CONTENT, section=section
    )
    _assert_veto_branch(merged, AMENITY_LIST_CONTENT, section)
    binding = _chunk_binding(merged, AMENITY_LIST_CONTENT)

    bound_values = {value for value, label in binding if label == named_label}
    assert bound_values, f"the chunk must bind a value to {named_label}"
    # Non-vacuous: the stolen figure is stated in the chunk, but not for this label.
    assert stolen not in bound_values, binding
    assert stolen in _chunk_evidence(merged)

    assert result["answer"] in CANNED_COPIES, config
    assert result["answer"] != draft, config
    assert audit.get("fallback_reason") == "numeric_ungrounded", config


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_c2_18_binding_veto_still_fires_for_a_wrong_subject_value_pair(
    monkeypatch, config, section
):
    """Positive control: a genuine mismatch is still refused after the merge.

    This chunk states 70.0 -- for ``ch-04``, not for ``ch-01`` -- so the presence
    authority passes the draft and only the subject binding can refuse it. Without
    this case, a change that switched the veto off entirely would leave every other
    case in this section green, which is exactly how a lock rots.
    """
    draft = "Căn CH-01 rộng 70,0 m²."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=VETO_CONTROL_CONTENT, section=section
    )
    _assert_veto_branch(merged, VETO_CONTROL_CONTENT, section)
    binding = _chunk_binding(merged, VETO_CONTROL_CONTENT)
    assert (Decimal("68.3"), "ch-01") in binding
    assert (Decimal("70.0"), "ch-04") in binding
    # Non-vacuous: the figure is present AND its real subject is bound -- to another
    # value. Presence and binding disagree, and the binding must win.
    assert Decimal("70.0") in _chunk_evidence(merged)

    assert result["answer"] in CANNED_COPIES, config
    assert audit.get("fallback_reason") == "numeric_ungrounded", config


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_c2_19_faithful_comma_separated_amenity_list_publishes_in_both_branches(
    monkeypatch, config, section
):
    """The over-veto regression: six faithful figures in ONE clause must publish.

    A comma does not end a clause, so all six labels and all six figures land in the
    same clause. A rule that applies one governing label to every figure in that
    clause refuses this answer -- and the answer is a correct quote of the retrieved
    chunk. This is the customer-visible half of the defect: the bot knows the answer
    and says it cannot answer.
    """
    result, merged, audit = _run_publication(
        monkeypatch,
        answer=AMENITY_LIST_ANSWER,
        chunk_content=AMENITY_LIST_CONTENT,
        section=section,
    )
    _assert_veto_branch(merged, AMENITY_LIST_CONTENT, section)
    binding = _chunk_binding(merged, AMENITY_LIST_CONTENT)
    for value, label in (
        (Decimal("434.2"), "nha_tre"),
        (Decimal("157.4"), "shcd_tang2"),
        (Decimal("381.6"), "be_boi"),
        (Decimal("134.3"), "gym"),
        (Decimal("43.6"), "yoga"),
        (Decimal("232.1"), "shcd_tang3"),
    ):
        assert (value, label) in binding
        assert value in _chunk_evidence(merged)

    assert result["answer"] not in CANNED_COPIES, config
    assert result["answer"] == AMENITY_LIST_ANSWER, config
    assert audit.get("fallback_reason") is None, config


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_c2_20_two_unit_codes_in_one_clause_publish_in_both_branches(
    monkeypatch, config, section
):
    """Two subjects and two figures in one clause, both correct: publish.

    The mirror of case 17. There the clause names a subject that refuses the figure;
    here it names two subjects and each one owns its own figure, so only a rule of
    the form "every named subject refuses it" gets both right.
    """
    draft = "Căn CH-03 rộng 68,3 m² và căn CH-04 rộng 70,1 m²."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=UNIT_CODES_CONTENT, section=section
    )
    _assert_veto_branch(merged, UNIT_CODES_CONTENT, section)
    binding = _chunk_binding(merged, UNIT_CODES_CONTENT)
    assert (Decimal("68.3"), "ch-03") in binding
    assert (Decimal("70.1"), "ch-04") in binding

    assert result["answer"] not in CANNED_COPIES, config
    assert result["answer"] == draft, config
    assert audit.get("fallback_reason") is None, config


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_c2_21_bound_subject_plus_balcony_in_one_clause_publishes_in_both_branches(
    monkeypatch, config, section
):
    """A bound unit area and a bound balcony area in one clause must both survive.

    ``ban_cong`` is a subject this chunk binds, so the clause names two subjects and
    each owns its own figure. Without that label in the chunk's own vocabulary the
    case is refused, which is why it is pinned here.
    """
    draft = "Căn CH-03 tại The Camellia rộng 68,3 m², ban công 5,2 m²."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=BALCONY_CONTENT, section=section
    )
    _assert_veto_branch(merged, BALCONY_CONTENT, section)

    # The publication decision is asserted before the binding vocabulary on purpose:
    # a gate without the balcony label refuses this correct answer, and the failure
    # must name that refusal rather than the harness's own vocabulary check.
    assert result["answer"] not in CANNED_COPIES, config
    assert result["answer"] == draft, config
    assert audit.get("fallback_reason") is None, config

    binding = _chunk_binding(merged, BALCONY_CONTENT)
    assert (Decimal("68.3"), "ch-03") in binding
    assert (Decimal("5.2"), "ban_cong") in binding


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_c2_22_correct_subject_written_with_spaces_publishes_in_both_branches(
    monkeypatch, config, section
):
    """The other side of case 17: the spaced spelling must not veto a TRUE statement.

    Case 17 proves the spaced label is refused when it steals another amenity's area.
    This proves the same spelling is accepted when it quotes its own, so the fix
    cannot be "refuse every spaced label" -- which would pass 17 and break the
    customer in the opposite direction.
    """
    draft = "Nhà trẻ tại The Camellia rộng 434,2 m²."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=AMENITY_LIST_CONTENT, section=section
    )
    _assert_veto_branch(merged, AMENITY_LIST_CONTENT, section)
    binding = _chunk_binding(merged, AMENITY_LIST_CONTENT)
    assert (Decimal("434.2"), "nha_tre") in binding

    assert result["answer"] not in CANNED_COPIES, config
    assert result["answer"] == draft, config
    assert audit.get("fallback_reason") is None, config


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_c2_23_multi_value_subject_publishes_in_both_branches(monkeypatch, config, section):
    """One subject with two legitimate values must accept either one.

    The chunk states a net area and a wall-to-wall area for the same unit, so the
    subject owns a SET of values. A rule that kept one value per subject would refuse
    the second figure, which is a correct quote of the chunk.
    """
    draft = "CH-01: 68,3 m² thông thủy / 73,8 m² tim tường."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=MULTI_CLAUSE_CONTENT, section=section
    )
    _assert_veto_branch(merged, MULTI_CLAUSE_CONTENT, section)
    binding = _chunk_binding(merged, MULTI_CLAUSE_CONTENT)
    bound_values = {value for value, label in binding if label == "ch-01"}
    assert bound_values == {Decimal("68.3"), Decimal("73.8")}, binding

    assert result["answer"] not in CANNED_COPIES, config
    assert result["answer"] == draft, config
    assert audit.get("fallback_reason") is None, config


# ---------------------------------------------------------------------------
# Round 4 / part A: a percentage is authorized by the chunk that states it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
@pytest.mark.parametrize(("variant", "chunk_content"), PCT_CHUNK_VARIANTS)
def test_a1_fabricated_percentage_is_canned_in_every_branch(
    monkeypatch, variant, chunk_content, config, section
):
    """A percentage no retrieved chunk states is a fabricated commitment: canned.

    The safety counterweight to case A2, and the reason the two live in one file: a
    chunk authority that licenses any percentage at all would answer this with a
    payment term the project never offered, and only a case that must be REFUSED can
    see that. The value is asserted absent from the chunk's own evidence, so this
    cannot pass by quoting a chunk that happens to state 88%.
    """
    result, merged, audit = _run_publication(
        monkeypatch, answer=PCT_DEPOSIT_FABRICATED, chunk_content=chunk_content, section=section
    )
    _assert_pct_branch(merged, chunk_content, section)
    assert Decimal("88") not in _chunk_evidence(merged), variant

    assert result["answer"] in CANNED_COPIES, (variant, config)
    assert audit.get("fallback_reason") == "numeric_ungrounded", (variant, config)


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
@pytest.mark.parametrize(("variant", "chunk_content"), PCT_CHUNK_VARIANTS)
def test_a2_faithful_percentage_publishes_in_every_branch(
    monkeypatch, variant, chunk_content, config, section
):
    """A percentage the retrieved chunk states must reach the customer.

    Before the fix every faithful percentage quote was canned, so a customer asking
    about the deposit schedule was told the figure could not be confirmed -- for a
    term the project's own document states in writing. The value is asserted present
    in the chunk's own evidence, which is what separates this case from A1.
    """
    result, merged, audit = _run_publication(
        monkeypatch, answer=PCT_DEPOSIT_FAITHFUL, chunk_content=chunk_content, section=section
    )
    _assert_pct_branch(merged, chunk_content, section)
    assert Decimal("5") in _chunk_evidence(merged), variant
    assert (Decimal("5"), "deposit") in _pct_evidence(merged), variant

    assert result["answer"] not in CANNED_COPIES, (variant, config)
    assert result["answer"] == PCT_DEPOSIT_FAITHFUL, (variant, config)
    assert audit.get("fallback_reason") is None, (variant, config)


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
@pytest.mark.parametrize(("variant", "chunk_content"), PCT_CHUNK_VARIANTS)
def test_a3_percentage_real_but_bound_to_another_policy_is_canned(
    monkeypatch, variant, chunk_content, config, section
):
    """5% is real, but the chunk states it for the DEPOSIT, not the rate: canned.

    The case presence alone cannot see. Both figures in play are genuine quotes of
    the chunk, so a rule that only asks "is this number in the chunk" publishes a
    loan rate the document never offered. The two assertions below state the defect
    exactly: the value IS in the chunk, and the chunk never attributes it to the
    policy the answer names.
    """
    result, merged, audit = _run_publication(
        monkeypatch, answer=PCT_RATE_MISATTRIBUTED, chunk_content=chunk_content, section=section
    )
    _assert_pct_branch(merged, chunk_content, section)
    assert Decimal("5") in _chunk_evidence(merged), variant
    assert (Decimal("5"), "rate") not in _pct_evidence(merged), variant

    assert result["answer"] in CANNED_COPIES, (variant, config)
    assert audit.get("fallback_reason") == "unsupported_figure", (variant, config)


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
@pytest.mark.parametrize(("variant", "chunk_content"), PCT_CHUNK_VARIANTS)
def test_a4_faithful_rate_publishes_in_every_branch(
    monkeypatch, variant, chunk_content, config, section
):
    """The mirror of A3: the same cue, this time quoting the chunk's own figure.

    A3 and A4 differ only in which of the chunk's two percentages the rate clause
    quotes, so together they pin that the veto reads the ATTRIBUTION and not merely
    the presence of a rate cue -- which is what keeps the fix from becoming "refuse
    every rate", a rule that would pass A3 and break the customer in A4.
    """
    result, merged, audit = _run_publication(
        monkeypatch, answer=PCT_RATE_FAITHFUL, chunk_content=chunk_content, section=section
    )
    _assert_pct_branch(merged, chunk_content, section)
    assert (Decimal("7.5"), "rate") in _pct_evidence(merged), variant

    assert result["answer"] not in CANNED_COPIES, (variant, config)
    assert result["answer"] == PCT_RATE_FAITHFUL, (variant, config)
    assert audit.get("fallback_reason") is None, (variant, config)


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
@pytest.mark.parametrize(("variant", "chunk_content"), PCT_CHUNK_VARIANTS)
def test_a6_faithful_rate_and_deposit_in_one_clause_both_publish(
    monkeypatch, variant, chunk_content, config, section
):
    """A rate and a deposit sharing one comma clause are each classified by their own segment.

    Classifying both figures by the whole clause tagged the faithful rate as a
    deposit and refused a correct answer. Each figure must be judged by the
    comma segment it sits in, so both publish.
    """
    answer = "Lãi suất vay là 7,5%/năm trong 18 tháng, đặt cọc 5% giá bán."
    result, merged, audit = _run_publication(
        monkeypatch, answer=answer, chunk_content=chunk_content, section=section
    )
    _assert_pct_branch(merged, chunk_content, section)
    assert (Decimal("7.5"), "rate") in _pct_evidence(merged), variant
    assert (Decimal("5"), "deposit") in _pct_evidence(merged), variant

    assert result["answer"] not in CANNED_COPIES, (variant, config)
    assert result["answer"] == answer, (variant, config)
    assert audit.get("fallback_reason") is None, (variant, config)


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
@pytest.mark.parametrize(("variant", "chunk_content"), PCT_MONEY_CHUNK_VARIANTS)
def test_a5_money_stated_by_the_chunk_is_still_not_licensed_by_it(
    monkeypatch, variant, chunk_content, config, section
):
    """The chunk states the price and no fact row does: still canned.

    The boundary of the new authority. A percentage may be licensed by the chunk
    because a payment term is a dimension a document legitimately states; an exact
    sale price exists only as a fact row, and a chunk merely mentioning a figure is
    not evidence of it. The value is asserted present in the chunk's own numeric
    evidence, so this cannot pass merely because the chunk never mentions the price.
    """
    result, merged, audit = _run_publication(
        monkeypatch, answer=PCT_MONEY_ANSWER, chunk_content=chunk_content, section=section
    )
    _assert_pct_branch(merged, chunk_content, section)
    assert PCT_MONEY_VALUE in _chunk_evidence(merged), variant

    assert result["answer"] in CANNED_COPIES, (variant, config)
    assert audit.get("fallback_reason") == "numeric_ungrounded", (variant, config)


# --- Round-4 part B: the one-line comma list (T13) ------------------------------
# The clause-wide veto degrades inside a comma list: "…, gym 434,2 m², …" is allowed
# because nha_tre, another label on the same line, owns 434,2 -- the figure is
# authorized without ever being bound to the subject the answer names. T13 recovers
# the pairing by judging the comma SEGMENT a figure sits in.
#
# The veto case alone does not pin that fix. "," is also the Vietnamese decimal mark,
# so a patch that splits on every comma tears "nha_tre 434,2 m²" into "nha_tre 434" and
# "2 m²"; no segment then carries a whole figure, the segment rule never fires, and the
# leak stays open while the code reads as if it were closed. The pair of B1 (veto) and
# B3 (publish) is what makes that patch fail: B1 catches the leak, B3 catches the
# over-veto a digit-blind splitter would cause on a faithful decimal figure.

B1_AMENITY_LIST_ANSWER = (
    "Diện tích tiện ích: nha_tre 434,2 m², shcd_tang2 157,4 m², be_boi 381,6 m², "
    "gym 434,2 m², yoga 43,6 m², shcd_tang3 232,1 m²"
)


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_b1_wrong_figure_inside_a_one_line_comma_list_is_vetoed(
    monkeypatch, config, section
):
    """A figure that is real but bound to the wrong subject must not ride the list.

    ``test_c2_19`` pins that this same list PUBLISHES when every figure is faithful.
    It does not pin the other direction, and the two are not the same rule: the
    clause-wide veto accepts 434,2 here because nha_tre -- a sibling label on the
    same line -- owns it, so the wrong pairing ``gym 434,2 m²`` was authorized by a
    figure that belongs to another amenity.

    The binding assertions state the defect rather than merely reporting a refusal:
    the chunk DOES license 434,2 (for nha_tre) and DOES price gym at 134,3, so a
    canned verdict here cannot be explained by the number being unknown to the chunk.
    """
    result, merged, audit = _run_publication(
        monkeypatch,
        answer=B1_AMENITY_LIST_ANSWER,
        chunk_content=AMENITY_LIST_CONTENT,
        section=section,
    )
    _assert_veto_branch(merged, AMENITY_LIST_CONTENT, section)
    binding = _chunk_binding(merged, AMENITY_LIST_CONTENT)
    assert (Decimal("434.2"), "nha_tre") in binding, config
    assert (Decimal("134.3"), "gym") in binding, config

    assert result["answer"] in CANNED_COPIES, config
    assert audit.get("fallback_reason") == "numeric_ungrounded", config


B6_AMENITY_PIPE_ANSWER = (
    "Diện tích tiện ích: nha_tre 434,2 m² | shcd_tang2 157,4 m² | be_boi 381,6 m² | "
    "gym 434,2 m² | yoga 43,6 m² | shcd_tang3 232,1 m²"
)


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_b6_wrong_figure_in_a_pipe_separated_list_is_vetoed(monkeypatch, config, section):
    """The "|" layout states the same leak as B1 and must be refused the same way.

    B1 and B6 differ ONLY in the separator the answer uses, against the same chunk, so
    the pair isolates the separator set rather than the misattribution. A pipe list
    held together as one clause names all six amenities, so the clause-wide rule
    accepts 434,2 on nha_tre's authority and the wrong pairing ships; the figure is
    only caught once "|" also ends a segment.

    Measured, not assumed: with "|" removed from the veto-side separator set this case
    is the only part B case that fails, which is what makes it the lock for that set.
    """
    result, merged, audit = _run_publication(
        monkeypatch,
        answer=B6_AMENITY_PIPE_ANSWER,
        chunk_content=AMENITY_LIST_CONTENT,
        section=section,
    )
    _assert_veto_branch(merged, AMENITY_LIST_CONTENT, section)
    binding = _chunk_binding(merged, AMENITY_LIST_CONTENT)
    assert (Decimal("434.2"), "nha_tre") in binding, config
    assert (Decimal("134.3"), "gym") in binding, config

    assert result["answer"] in CANNED_COPIES, config
    assert audit.get("fallback_reason") == "numeric_ungrounded", config


# A "/" that is NOT a list separator, in two of the shapes ordinary Vietnamese prose
# uses it: a date and a per-capita unit. Both answers are faithful and both carry a
# figure the chunk states for CH-03, so they exercise the veto path rather than sailing
# past it on an ungrounded number.
B7_SLASH_IN_PLAIN_TEXT_ANSWERS = (
    ("date", "Căn CH-03 bàn giao ngày 01/02/2026, diện tích 68,3 m²."),
    ("compound unit", "Căn CH-03 có 68,3 m²/người theo tiêu chuẩn."),
)


@pytest.mark.parametrize(("label", "draft"), B7_SLASH_IN_PLAIN_TEXT_ANSWERS)
@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_b7_slash_in_plain_text_still_publishes(monkeypatch, label, draft, config, section):
    """A "/" inside ordinary prose must not withhold a correct answer.

    Withholding a true statement is worse than the leak the veto-side separators close,
    so the set that ends a segment is a customer-visible decision and needs a case on
    the refusing side of it. "01/02/2026" is not a list: it is a date, and splitting it
    into "01", "02", "2026" is harmless only because none of those fragments carries a
    subject label. This case pins that it stays harmless.

    B7 and B8 travel together and neither is sufficient alone: B8 by itself is passed by
    a patch that drops "/" from the separator set entirely (the leak returns, B8 reddens,
    but nothing here notices), and B7 by itself is passed by a patch that widens the set
    until faithful prose is refused.
    """
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=BALCONY_CONTENT, section=section
    )
    _assert_veto_branch(merged, BALCONY_CONTENT, section)
    binding = _chunk_binding(merged, BALCONY_CONTENT)
    assert (Decimal("68.3"), "ch-03") in binding, (label, config)

    assert result["answer"] not in CANNED_COPIES, (label, config)
    assert result["answer"] == draft, (label, config)
    assert audit.get("fallback_reason") is None, (label, config)


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_b8_slash_joins_a_wrong_pairing_and_is_vetoed(monkeypatch, config, section):
    """The same separator must still catch a wrong (subject, figure) pair it creates.

    The clause names two subjects, so the clause-wide rule leaves it alone: 5,2 m² is
    refused by ch-03 but accepted by ban_cong, and a figure is a contradiction only when
    EVERY subject the clause names refuses it. Held together the pairing therefore ships
    -- which is the leak. Only once "/" ends a segment does "Căn CH-03 rộng 5,2 m²" stand
    alone as one subject priced against its own value, and the wrong pairing is caught.

    Measured, not assumed: dropping "/" from the veto-side separator set reddens exactly
    this case and B6, and nothing else, which is what makes the pair the lock for the set.
    """
    draft = "Căn CH-03 rộng 5,2 m² / ban công 68,3 m²."
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=BALCONY_CONTENT, section=section
    )
    _assert_veto_branch(merged, BALCONY_CONTENT, section)
    binding = _chunk_binding(merged, BALCONY_CONTENT)
    assert (Decimal("68.3"), "ch-03") in binding, config
    assert (Decimal("5.2"), "ban_cong") in binding, config

    assert result["answer"] in CANNED_COPIES, config
    assert audit.get("fallback_reason") == "numeric_ungrounded", config


# --- Round-5 part A: the money token "đ" carries a word boundary (T17) ----------
# The money unit "đ" was matched with no boundary, so any number in front of a
# Vietnamese word beginning with "đ" was read as a CURRENCY figure: "1 điều", "6 đợt",
# "2 đầu". A currency figure may not be authorized by prose, so the phantom money
# figure became an orphan and the WHOLE answer was withheld even though every real
# figure in it was a faithful quote. Measured on the real corpus by the round-4
# verifier: 45 of 336 chunks, 13.4%.
#
# Both directions are locked, and the second is the one that keeps the fix honest.
# The "đ" branch exists to CLOSE a fail-open: before it, "2850000000 đ" raised
# InvalidOperation inside normalization, the number was dropped before _is_money_claim
# ever saw it, and a fabricated price reached the customer. A boundary that is too
# aggressive would reopen exactly that hole by turning the real token back into
# unit=None, so A2 asserts the unit SURVIVED rather than merely that the figure still
# counts as money -- with unit=None a 2.85e9 figure is money anyway, on magnitude
# alone, and an A2 that only checked _is_money_claim would pass with the token gone.
T17_D_WORDS = (
    ("dieu", "khoản 1 điều 152 luật nhà ở 2023"),
    ("dot", "chia thành 6 đợt thanh toán"),
    ("don", "mỗi 2 đơn vị"),
    ("dau", "từ 3 đầu mối"),
    ("den", "từ 4 đến 6 tháng"),
    ("dinh", "có 5 đính kèm"),
)

# Every unit that must still make a figure money. The first four are the "đ" family the
# boundary was added to; the last four pin the units that never had one, so a future
# edit to this alternation cannot quietly drop a unit while A2 stays green.
T17_MONEY_UNITS = (
    ("dong", "2850000000 đồng"),
    ("d", "2850000000 đ"),
    ("d_glued", "2.850.000.000đ"),
    ("vnd", "2850000000 vnđ"),
    ("ty", "2,85 tỷ"),
    ("ti", "2,85 tỉ"),
    ("trieu", "2.850 triệu"),
    ("ngan", "2.850.000 ngàn"),
    ("nghin", "2.850.000 nghìn"),
)

# Two lines so 2% appears under both an "other" cue (phí bảo trì) and a "payment" cue
# (tiến độ thanh toán). That is what lets one chunk license both faithful answers below
# without either of them leaning on the other's cue.
T17_MAINTENANCE_CONTENT = (
    "Chinh sach phi tai The Camellia.\n"
    "Phi bao tri 2% gia tri can ho, thu mot lan khi nhan ban giao.\n"
    "Tien do thanh toan chia thanh 6 dot, moi dot 2% gia tri can ho.\n"
)

# Both answers are faithful to the chunk above and both name a word starting with "đ"
# after a number. The first carries the legal citation whose "khoản 1 điều 152" produced
# the phantom figure; the second carries "6 đợt" in the answer itself.
T17_FAITHFUL_ANSWERS = (
    (
        "legal_citation",
        "Phí bảo trì tại The Camellia là 2% giá trị căn hộ (khoản 1 điều 152 luật nhà ở 2023).",
    ),
    (
        "dot_count",
        "Tiến độ thanh toán chia thành 6 đợt, mỗi đợt 2% giá trị căn hộ.",
    ),
)


@pytest.mark.parametrize(("label", "phrase"), T17_D_WORDS)
def test_t17_a1_a_number_before_a_d_word_is_not_a_money_token(label, phrase):
    """A number in front of a Vietnamese "đ" word must yield no figure at all.

    This is the whole defect in one assertion. "khoản 1 điều 152" states a legal
    article, not a price, but the unbounded "đ" read the "1" as one đồng -- a currency
    figure, which no prose may authorize, so it became an orphan and canned an answer
    that quoted the chunk faithfully.
    """
    assert _extract_amount_matches(phrase) == [], label


@pytest.mark.parametrize(("label", "text"), T17_MONEY_UNITS)
def test_t17_a2_a_real_money_unit_keeps_its_figure(label, text):
    """The boundary must not reopen the fail-open it was added to close.

    Asserting _is_money_claim alone would not prove this: a 2.85e9 figure is money on
    magnitude even with unit=None, so the token could be gone and the case would still
    pass. The unit itself is asserted present, which is the property the "đ" branch
    was added for.
    """
    spans = _extract_amount_matches(text)
    assert len(spans) == 1, (label, spans)
    amount, unit, _start, _end = spans[0]
    assert unit is not None, (label, spans)
    assert unit in _CURRENCY_UNITS, (label, spans)
    assert _is_money_claim(amount, unit), (label, spans)


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
@pytest.mark.parametrize(("label", "draft"), T17_FAITHFUL_ANSWERS)
def test_t17_a3_a_faithful_answer_naming_a_d_word_publishes(
    monkeypatch, label, draft, config, section
):
    """End to end: the phantom figure must no longer withhold a faithful answer.

    The figure the customer asked about is a real percentage the chunk states, so the
    publication decision must not be decided by an unrelated "đ" word in the same
    sentence. Asserted present in the chunk's own percentage evidence, which is what
    separates this case from a case that would pass because the gate stopped looking.
    """
    result, merged, audit = _run_publication(
        monkeypatch, answer=draft, chunk_content=T17_MAINTENANCE_CONTENT, section=section
    )
    _assert_veto_branch(merged, T17_MAINTENANCE_CONTENT, section)
    assert any(amount == Decimal("2") for amount, _cue in _pct_evidence(merged)), label

    assert result["answer"] not in CANNED_COPIES, (label, config)
    assert result["answer"] == draft, (label, config)
    assert audit.get("fallback_reason") is None, (label, config)


# --- Round-5 part B: the binding walk reads every figure of a range (T18) --------
# The real registry writes a range with the subject named ONCE, before the colon:
# "- be_boi: 300 m² (A1) đến 500 m² (D);". The binding walk stops at a separator, and
# ")" is one, so the second figure's own segment was " đến " -- no subject -- and only
# 300 was ever bound. An answer that quoted the range correctly then had 500 refused by
# be_boi and was canned, which is the forbidden direction: a faithful answer withheld.
#
# The fix reads the nearest preceding "label:" as a fallback governor, gated on the
# label being one this chunk ALREADY prices through the walk. The gate is the part that
# must not be lost, so B2 is written against it: widening the binding must not turn a
# figure the chunk never states into a publishable one.
T18_SOLEIL_CONTENT = (
    "Tien ich The Soleil.\n"
    "- be_boi: 300 m² (A1) đến 500 m² (D);\n"
    "- gym: 250 m²;\n"
    "- yoga: 120 m²;\n"
)

T18_RANGE_ANSWER = "Bể bơi tại The Soleil có diện tích 300 m² (tòa A1) đến 500 m² (tòa D)."
T18_FABRICATED_ANSWER = "Bể bơi tại The Soleil có diện tích 900 m² (tòa A1)."
T18_SINGLE_ANSWER = "Bể bơi tại The Soleil có diện tích 300 m² (tòa A1)."


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_t18_b1_a_range_the_chunk_states_publishes(monkeypatch, config, section):
    """Both ends of the chunk's own range must be bound to the subject it names.

    The two assertions on the binding are the point of the case: 300 comes from the
    separator walk and 500 only from the label-intro fallback, so a regression that
    drops the fallback reddens this case rather than hiding behind the first figure.
    """
    result, merged, audit = _run_publication(
        monkeypatch,
        answer=T18_RANGE_ANSWER,
        chunk_content=T18_SOLEIL_CONTENT,
        section=section,
    )
    _assert_t18_branch(merged, T18_SOLEIL_CONTENT, section)
    binding = _chunk_binding(merged, T18_SOLEIL_CONTENT)
    assert (Decimal("300"), "be_boi") in binding, config
    assert (Decimal("500"), "be_boi") in binding, config

    assert result["answer"] not in CANNED_COPIES, config
    assert result["answer"] == T18_RANGE_ANSWER, config
    assert audit.get("fallback_reason") is None, config


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_t18_b2_a_figure_outside_the_range_is_still_canned(monkeypatch, config, section):
    """The counterweight: widening the binding must not license a figure not in the chunk.

    900 m² appears nowhere in the chunk, so no reading of the range may authorize it.
    The gate that keeps this honest is the "label in priced" condition on the fallback;
    without it a label could be invented for any unbound figure and the veto would stop
    firing. Measured: this case is canned both with and without the fallback, which is
    what makes it a counterweight rather than a second copy of B1.
    """
    result, merged, audit = _run_publication(
        monkeypatch,
        answer=T18_FABRICATED_ANSWER,
        chunk_content=T18_SOLEIL_CONTENT,
        section=section,
    )
    _assert_t18_branch(merged, T18_SOLEIL_CONTENT, section)
    binding = _chunk_binding(merged, T18_SOLEIL_CONTENT)
    assert (Decimal("900"), "be_boi") not in binding, config

    assert result["answer"] in CANNED_COPIES, config
    assert audit.get("fallback_reason") == "numeric_ungrounded", config


@pytest.mark.parametrize(("config", "section"), AREA_EVIDENCE_CONFIGS)
def test_t18_b3_a_single_figure_the_chunk_states_publishes(monkeypatch, config, section):
    """Boundary of the fix: the first figure of the range already worked, and must keep working.

    The separator walk alone binds 300, so this case is green before and after T18. It is
    here so that a regression that breaks the walk while adding the fallback -- the two
    read the same colon -- cannot pass as "the fallback still works".
    """
    result, merged, audit = _run_publication(
        monkeypatch,
        answer=T18_SINGLE_ANSWER,
        chunk_content=T18_SOLEIL_CONTENT,
        section=section,
    )
    _assert_t18_branch(merged, T18_SOLEIL_CONTENT, section)
    binding = _chunk_binding(merged, T18_SOLEIL_CONTENT)
    assert (Decimal("300"), "be_boi") in binding, config

    assert result["answer"] not in CANNED_COPIES, config
    assert result["answer"] == T18_SINGLE_ANSWER, config
    assert audit.get("fallback_reason") is None, config
