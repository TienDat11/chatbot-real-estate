"""End-to-end workflow checks for exact-unit evidence presentation isolation.

Financial publication now runs through the P0-03C per-claim matcher, so the
positive fixtures here are REAL selected ``facts`` rows — fact id, VND unit,
exact/confirmed quality and a current published source document plus active
same-project campaign — produced by the real ``build_fact_evidence``/SQL leg
seam. Rows without that lineage (a bare ``confirmed=True`` dict, an offer-shaped
row) can no longer supply a price and are pinned as negatives.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

import api.application.pipelines.workflow as workflow_module
import api.application.services.merge as merge_module
from api.application.pipelines.workflow import (
    LOW_GROUNDING_MESSAGE,
    NO_CONFIRMED_UNIT_PRICE,
    PRICE_LOOKUP_UNCERTAIN,
    RagQueryPipeline,
    RagQueryWorkflow,
)
from api.application.services.generate import build_messages
from api.application.services.project_context import ProjectContext
from api.application.services.rag_leg import RagLegResult
from api.application.services.sql_leg import (
    ConfirmedPriceProbe,
    SqlLegResult,
    build_fact_evidence,
)
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.guard_output import guard_output as run_guard_output
from api.domain.services.rewrite import RoutedResult
from api.domain.value_objects.constants import SSE_EVENT_FACTS, SSE_EVENT_IMAGES

_ACTIVE_CATALOGUE = (("camellia", "Camellia"),)
_TODAY = date.today()
_START = _TODAY - timedelta(days=30)


def _project_context(project_key="camellia") -> ProjectContext:
    return ProjectContext(project_key, "normal", _ACTIVE_CATALOGUE)


class _Reranker:
    async def rerank(self, query, chunks):
        return chunks


_CONTENT = "Camellia có vườn cây xanh và lối dạo bộ."
_CHUNK = {"id": "camellia-amenities", "content": _CONTENT, "score": 0.91}
_REGISTRY_RECORD = {
    "chunk_id": _CHUNK["id"],
    "registry_content": _CONTENT,
    "doc_id": "camellia-doc",
    "title": "Hồ sơ tiện ích Camellia",
    "section": "Tiện ích",
    "kind": "project",
    "project_key": "camellia",
    "status": "published",
    "effective_from": date(2020, 1, 1),
    "effective_to": None,
}

_DOC = "price-camellia-2026q3"
_CAMPAIGN = "camellia-2026q3"


def _raw_fact(
    subject_key: str,
    fact_id: int,
    *,
    value: object = 1_000_000,
    quality: str = "exact",
    trust: str | None = "confirmed",
    fact_key: str = "price_vnd",
    unit: str | None = "vnd",
    policy_key: str | None = "htls",
    lineage: bool = True,
    **overrides,
) -> dict:
    """One raw ``facts`` row in exactly the shape ``build_sql`` returns.

    ``lineage=False`` models a row the registry cannot vouch for (no published
    source document, no active same-project campaign) — the shape every
    pre-P0-03C fixture used, and the shape that must never authorize a figure.
    """
    row = {
        "fact_id": fact_id,
        "subject_key": subject_key,
        "subject_type": "unit",
        "display_name": None,
        "fact_key": fact_key,
        "policy_key": policy_key,
        "campaign_key": _CAMPAIGN,
        "value_num": value,
        "value_text": None,
        "unit": unit,
        "quality": quality,
        "range_min": None,
        "range_max": None,
        "effective_from": _START.isoformat(),
        "effective_to": None,
        "source_doc_id": _DOC,
        "source_chunk_id": None,
        "trust_level": trust,
        "source_doc_status": "published",
        "source_doc_effective_from": _START.isoformat(),
        "source_doc_effective_to": None,
        "source_doc_project_key": "camellia",
        "campaign_project_key": "camellia",
        "campaign_status": "active",
        "campaign_effective_from": _START.isoformat(),
        "campaign_effective_to": None,
        "campaign_source_doc_id": _DOC,
        "campaign_source_doc_status": "published",
        "campaign_source_doc_effective_from": _START.isoformat(),
        "campaign_source_doc_effective_to": None,
        "campaign_source_doc_project_key": "camellia",
    }
    if not lineage:
        for key in (
            "source_doc_status",
            "source_doc_project_key",
            "source_doc_effective_from",
            "campaign_project_key",
            "campaign_status",
            "campaign_effective_from",
            "campaign_source_doc_id",
            "campaign_source_doc_status",
            "campaign_source_doc_project_key",
            "campaign_source_doc_effective_from",
        ):
            row[key] = None
    row.update(overrides)
    return row


def _offer_row(subject_key: str, *, price: int = 1_000_000) -> dict:
    """A ``v_unit_offers`` row: shaped like a price, proven by nothing."""
    return {
        "subject_id": 7,
        "subject_key": subject_key,
        "policy_key": "htls",
        "price_vnd": price,
        "deposit_pct": 30,
        "term_months": 120,
        "interest_rate_pct": 12,
        "required_down_payment_vnd": 300_000_000,
        "loan_amount_vnd": 700_000_000,
        "monthly_principal_vnd": 5_833_333,
        "monthly_interest_estimate_vnd": 7_000_000,
    }


def _image(image_id: str, *, kind: str, title: str, project_key="camellia", unit_code=None, reason="") -> dict:
    return {
        "image_id": image_id,
        "kind": kind,
        "title": title,
        "project_key": project_key,
        "unit_code": unit_code,
        "reason": reason,
    }


def _registry_pool():
    class Connection:
        async def fetch(self, sql, ids):
            return [_REGISTRY_RECORD]

        async def fetchrow(self, *args, **kwargs):
            return None

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    async def pool():
        return Pool()

    return pool


def _probe(*, confirmed: frozenset[str] = frozenset(), any_confirmed: bool | None = None,
           completed: bool = True) -> ConfirmedPriceProbe:
    return ConfirmedPriceProbe(
        completed=completed,
        error=None if completed else "timeout",
        confirmed_units=confirmed,
        any_confirmed=bool(confirmed) if any_confirmed is None else any_confirmed,
    )


def _run(
    monkeypatch,
    query: str,
    facts: list[dict],
    images: list[dict],
    project_key: str = "camellia",
    source_evidence: list[dict] | None = None,
    source_evidence_source: str = "facts",
    capture_tokens: bool = False,
    answer: str = "answer",
    use_real_output_guard: bool = False,
    guard_timeout: bool = False,
    price_probe: ConfirmedPriceProbe | None = None,
):
    """Drive the real workflow with every external call stubbed.

    ``facts`` / ``source_evidence`` are RAW sql-leg rows: the harness runs the
    production ``build_fact_evidence`` over them, so the selected prompt rows,
    the UI projection and the lineage the matcher reads are the real ones.
    """
    events: list[tuple[str, dict]] = []
    generated: list[object] = []
    guarded: list[list[dict]] = []
    rows = source_evidence if source_evidence is not None else facts

    async def guard_input(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=False)

    async def rewrite_query(clean, history, as_of_iso):
        return RoutedResult(
            rewritten=clean,
            routing={"needs_rag": True, "needs_sql": True, "structured_path": "none", "needs_geo": False},
            sql_spec={"source": source_evidence_source},
            hl_keywords=[],
            ll_keywords=[],
            high_stakes=False,
            as_of=None,
        )

    async def run_rag_leg(*args):
        return RagLegResult(chunks=[dict(_CHUNK)])

    async def run_sql_leg(spec, as_of, query, project_key):
        return SqlLegResult(
            rows=build_fact_evidence(rows, source_evidence_source, as_of),
            meta={"source": source_evidence_source, "mode": "spec"},
        )

    async def search_images(query, project_key=None):
        return images

    async def stream_answer(merged, history, high_stakes):
        # build_messages is the production writer of the prompt-evidence rows
        # output_gate/_prompt_evidence_integrity gates publication on.
        build_messages(merged, history)
        generated.append(merged)
        yield answer

    async def write_audit(entry):
        return None

    async def guard_output(answer, observed_facts, sources, routing, meta=None):
        guarded.append(observed_facts)
        if guard_timeout:
            raise asyncio.TimeoutError
        if use_real_output_guard:
            return await run_guard_output(answer, observed_facts, sources, routing, meta=meta)
        return OutputGuardResult(confidence="MEDIUM", requires_review=False, verdicts={})

    async def probe(project, unit_codes=None, **kwargs):
        return price_probe if price_probe is not None else _probe()

    monkeypatch.setattr(workflow_module, "guard_input", guard_input)
    monkeypatch.setattr(workflow_module, "rewrite_query", rewrite_query)
    monkeypatch.setattr(workflow_module, "run_rag_leg", run_rag_leg)
    monkeypatch.setattr(workflow_module, "run_sql_leg", run_sql_leg)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _Reranker())
    monkeypatch.setattr(workflow_module, "search_images", search_images)
    monkeypatch.setattr(workflow_module, "stream_answer", stream_answer)
    monkeypatch.setattr(workflow_module, "guard_output", guard_output)
    monkeypatch.setattr(workflow_module, "write_audit", write_audit)
    monkeypatch.setattr(workflow_module, "probe_confirmed_price", probe)
    monkeypatch.setattr(merge_module, "get_ro_pool", _registry_pool())

    async def go():
        workflow = RagQueryWorkflow(on_event=lambda name, data: events.append((name, data)))
        return await workflow.run(query=query, project_context=_project_context(project_key), session_id=None, history=[])
    result = asyncio.run(go())
    # A blocked turn publishes NO evidence frame at all (publish_original is the
    # only gate on SSE_EVENT_FACTS/SSE_EVENT_IMAGES, see
    # test_publication_authority.py::test_omitted_prompt_rows_block_selected_evidence_and_answer),
    # so a missing frame is reported as None rather than an empty payload.
    fact_frame = next((data for name, data in events if name == SSE_EVENT_FACTS), None)
    image_frame = next((data for name, data in events if name == SSE_EVENT_IMAGES), None)
    token_frames = [data for name, data in events if name == "token"]
    if capture_tokens:
        return result, fact_frame, image_frame, generated, guarded, token_frames
    return result, fact_frame, image_frame, generated, guarded


def test_exact_unit_presentation_filters_sse_and_done_but_keeps_full_evidence(monkeypatch):
    facts = [
        _raw_fact("unit:camellia/CH-13A", 101),
        _raw_fact("unit:camellia/CH-11", 102),
    ]
    images = [_image("exact", kind="matbang", title="Mặt bằng CH-13A", unit_code="CH-13A")]
    result, fact_frame, image_frame, generated, guarded = _run(monkeypatch, "Thông tin căn CH-13A?", facts, images)
    presented = [facts[0]]
    assert [fact["subject"] for fact in result["facts"]] == ["unit:camellia/CH-13A"]
    assert [fact["subject"] for fact in fact_frame["facts"]] == ["unit:camellia/CH-13A"]
    assert [image["image_id"] for image in image_frame["images"]] == ["exact"]
    assert [fact["subject"] for fact in generated[0].facts] == ["unit:camellia/CH-13A", "unit:camellia/CH-11"]
    assert [fact["subject"] for fact in guarded[0]] == ["unit:camellia/CH-13A", "unit:camellia/CH-11"]


def test_exact_unit_without_project_scope_fails_closed(monkeypatch):
    pipeline = RagQueryPipeline()
    with pytest.raises(TypeError, match="project_context"):
        asyncio.run(pipeline.run(query="Thông tin CH-13A?", session_id=None, history=[]))


def test_exact_unit_generic_type_prices_are_not_presented_in_answer_or_sse(monkeypatch):
    # Selected project rows that are structurally sound but carry no lineage:
    # the draft's unit-type price bands authenticate against nothing.
    facts = [
        _raw_fact("project:camellia", 11, value="2.1–2.4 tỷ", fact_key="policy_price_ranges", unit=None),
        _raw_fact("project:camellia", 12, value="2.8–3.2 tỷ", fact_key="unit_count", unit=None),
    ]
    generated_answer = "Căn Studio có giá 2.1–2.4 tỷ, căn 1PN có giá 2.8–3.2 tỷ."
    result, fact_frame, image_frame, generated, guarded, token_frames = _run(
        monkeypatch, "Giá căn CH-13A?", facts, [], capture_tokens=True, answer=generated_answer
    )
    assert result["answer"] == NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-13A")
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    assert all("2.1–2.4 tỷ" not in str(frame) and "2.8–3.2 tỷ" not in str(frame) for frame in token_frames)
    # A blocked turn publishes NO evidence frame at all, not an empty one:
    # publish_original is the only gate on SSE_EVENT_FACTS/SSE_EVENT_IMAGES
    # (workflow.py:907-922) and an empty frame would assert "there is no
    # evidence" for a turn whose evidence exists but is unverifiable.
    assert fact_frame is None
    assert image_frame is None
    assert result["facts"] == []
    assert result["images"] == []


def test_exact_unit_price_comparison_requires_price_for_each_unit(monkeypatch):
    facts = [_raw_fact("unit:camellia/CH-13A", 101, lineage=False)]
    result, *_ = _run(monkeypatch, "So sánh giá căn CH-13A và CH-11", facts, [], answer="CH-13A có giá 2 tỷ, CH-11 khoảng 3 tỷ.")
    # One complete template sentence per missing canonical code, query order,
    # space-separated — never a joined code list inside a single sentence.
    assert result["answer"] == (
        NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-13A")
        + " "
        + NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-11")
    )
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_exact_unit_grounded_price_is_not_overridden(monkeypatch):
    facts = [_raw_fact("unit:camellia/CH-13A", 101, lineage=False)]
    result, *_ = _run(
        monkeypatch, "Giá căn CH-13A?", facts, [],
        source_evidence=[_raw_fact("unit:camellia/CH-13A", 101)],
        answer="Căn CH-13A có giá 1.000.000 đồng.",
    )
    assert result["answer"] == "Căn CH-13A có giá 1.000.000 đồng."
    assert result["confidence"] == "MEDIUM"
    assert result["requires_review"] is False


def test_exact_price_timeout_keeps_guard_verdicts_and_never_publishes_model_answer(monkeypatch):
    facts = [_raw_fact("unit:camellia/CH-11", 102, lineage=False)]
    # guard_timeout raises inside the harness' own guard_output fake: a
    # monkeypatch applied before _run would be overwritten by the harness
    # patch, so the timeout would never fire.
    result, fact_frame, image_frame, *_rest, token_frames = _run(
        monkeypatch, "Giá căn CH-13A?", facts, [], answer="Giá căn CH-13A là 2 tỷ.",
        capture_tokens=True, guard_timeout=True,
    )
    # An unavailable verifier is uncertainty, never a claim of absence.
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    assert [frame["text"] for frame in token_frames] == [PRICE_LOOKUP_UNCERTAIN]
    assert fact_frame is None
    assert image_frame is None
    assert result["facts"] == []
    assert result["images"] == []


def test_estimated_exact_unit_price_provenance_is_not_offered_as_confirmed(monkeypatch):
    result, *_ = _run(
        monkeypatch, "Giá căn CH-13A?", [_raw_fact("unit:camellia/CH-13A", 101, lineage=False)], [],
        source_evidence=[_raw_fact("unit:camellia/CH-13A", 101, quality="approx", trust="estimate", lineage=False)],
        answer="CH-13A giá 1.000.000 đồng.",
    )
    assert result["answer"] == NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-13A")
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_offer_shaped_row_fails_prompt_evidence_integrity(monkeypatch):
    """A ``v_unit_offers`` row has no fact id, no interval and no lineage.

    D5: it is not a priced fact. Because it cannot even be a valid current
    prompt row, the turn is a provenance-integrity block: neither the offer's
    own number nor a "no confirmed price" claim may ship off it.
    """
    display_facts = [_raw_fact("unit:camellia/CH-13A", 101, lineage=False)]
    result, fact_frame, image_frame, *_ = _run(
        monkeypatch, "Giá căn CH-13A?", display_facts, [],
        source_evidence_source="v_unit_offers",
        source_evidence=[_offer_row("unit:camellia/CH-13A")],
        answer="CH-13A giá 1.000.000 đồng.",
    )
    assert result["answer"] == LOW_GROUNDING_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    assert fact_frame is None
    assert image_frame is None
    assert result["facts"] == []


def test_offer_shaped_fact_without_lineage_reports_absent_confirmed_price(monkeypatch):
    """D5: a null-trust, lineage-less row authorizes nothing, so absence is said."""
    result, *_ = _run(
        monkeypatch, "Giá căn CH-13A?", [_raw_fact("unit:camellia/CH-13A", 101, lineage=False)], [],
        source_evidence=[_raw_fact("unit:camellia/CH-13A", 101, trust=None, lineage=False)],
        answer="CH-13A giá 1.000.000 đồng.",
    )
    assert result["answer"] == NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-13A")
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_seed_offer_without_confirmed_trust_is_not_exact_price_evidence(monkeypatch):
    result, *_ = _run(
        monkeypatch, "Giá căn CH-13A?", [_raw_fact("unit:camellia/CH-13A", 101, lineage=False)], [],
        source_evidence=[_raw_fact("unit:camellia/CH-13A", 101, trust="estimate", lineage=False)],
        answer="CH-13A giá 1.000.000 đồng.",
    )
    assert result["answer"] == NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-13A")
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_seed_estimate_shaped_as_exact_offer_cannot_supply_exact_price(monkeypatch):
    result, *_ = _run(
        monkeypatch, "Giá căn CH-13A?", [_raw_fact("unit:camellia/CH-13A", 101, lineage=False)], [],
        source_evidence_source="v_unit_offers",
        source_evidence=[_offer_row("unit:camellia/CH-13A", price=999_000_000)],
        answer="CH-13A giá 1.000.000 đồng.",
    )
    assert result["answer"] == LOW_GROUNDING_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_exact_unit_price_with_project_scope_emits_only_unavailable_answer(monkeypatch):
    result, fact_frame, image_frame, generated, guarded, token_frames = _run(
        monkeypatch, "Giá căn CH-13A?", [_raw_fact("unit:camellia/CH-13A", 101, lineage=False)],
        [_image("plan", kind="matbang", title="Mặt bằng CH-13A")],
        capture_tokens=True, answer="Căn CH-13A giá khoảng 2 tỷ.",
    )
    assert result["answer"] == NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-13A")
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    assert all("2 tỷ" not in str(frame) for frame in token_frames)
    # Blocked turn: no evidence frame is emitted at all (see the sibling test
    # test_exact_unit_generic_type_prices_are_not_presented_in_answer_or_sse).
    assert fact_frame is None
    assert image_frame is None
    assert result["facts"] == []
    assert result["images"] == []


def test_exact_offer_guard_ignores_unrelated_generic_ranges(monkeypatch):
    exact = _raw_fact("unit:camellia/CH-13A", 101)
    generic = _raw_fact("unit-type:camellia/Studio", 103, value="2.1-2.4 ty", lineage=False)
    result, _, _, generated, guarded = _run(
        monkeypatch,
        "Giá căn CH-13A?",
        [exact, generic],
        [],
        source_evidence=[exact],
        answer="CH-13A giá 1.000.000 đồng; căn Studio có giá 2.1 tỷ.",
        use_real_output_guard=True,
    )
    assert result["answer"] != "CH-13A giá 1.000.000 đồng; căn Studio có giá 2.1 tỷ."
    assert result["confidence"] == "LOW"
    # The verifier only ever saw the requested unit's proven row.
    assert [fact["subject"] for fact in generated[0].facts] == [exact["subject_key"]]
    assert [fact["subject"] for fact in guarded[0]] == [exact["subject_key"]]


def test_exact_unit_price_without_project_scope_is_unavailable(monkeypatch):
    pipeline = RagQueryPipeline()
    with pytest.raises(TypeError, match="project_context"):
        asyncio.run(pipeline.run(query="Giá căn CH-13A?", session_id=None, history=[]))
