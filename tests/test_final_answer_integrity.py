"""P0-05 — the visible answer, the token stream and the audit hash are one string.

The invariant this file proves, for every branch that ships text::

    sha256(expected_bytes)            == audit.answer_hash
    concat(token.data.text).encode()  == returned/done.answer.encode()
                                      == persisted assistant_content.encode()

Nothing here asserts on a mock's forwarding or on source text. The real
``RagQueryWorkflow``, the real ``RagQueryPipeline``/``RagQueryPipelineConv``
facades and the real ``GlobalAssistantService`` core are driven end to end;
only the external seams (LLM provider, registry pool, history) are replaced,
and the LLM stream is still funnelled through the real ``build_messages`` so
publication authority stays non-fabricated.

The audit recorder snapshots a deep copy at write entry and can be held on an
``asyncio.Event``; while it is held the test asserts that NO token, source,
fact, image or place event has escaped. Waiting is event coordination with a
bounded iteration budget, never a wall-clock sleep.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from datetime import date, timedelta

import pytest

import api.application.pipelines.workflow as workflow_module
import api.application.services.global_assistant_service as ga_module  # noqa: F401
import api.application.services.merge as merge_module
from api.application.pipelines.conv_workflow import RagQueryPipelineConv
from api.application.pipelines.workflow import (
    RagQueryPipeline,
)
from api.application.services.generate import GenerationProviderError, build_messages
from api.application.services.global_assistant_service import (
    GlobalAssistantAnswerIntegrityError,
    GlobalAssistantInput,
    GlobalAssistantService,
)
from api.application.services.output_sanitizer import sanitize_answer_text
from api.application.services.project_config import ProjectRegistryRecord
from api.application.services.project_context import ProjectContext
from api.application.services.rag_leg import RagLegResult
from api.application.services.sql_leg import (
    ConfirmedPriceProbe,
    SqlLegResult,
    build_fact_evidence,
)
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import guard_output
from api.domain.services.rewrite import RoutedResult
from api.domain.value_objects.constants import (
    SSE_EVENT_FACTS,
    SSE_EVENT_IMAGES,
    SSE_EVENT_PLACES,
    SSE_EVENT_SOURCES,
    SSE_EVENT_TOKEN,
)

PROJECT = ProjectContext("camellia", "normal", (("camellia", "Camellia"), ("soleil", "Soleil")))
START = (date.today() - timedelta(days=30)).isoformat()
DOC = "price-camellia-2026q3"
CAMPAIGN = "camellia-2026q3"
CONTENT = "Camellia có vườn cây xanh và lối dạo bộ."
DESCRIPTIVE = "Camellia có vườn cây xanh theo Hồ sơ tiện ích Camellia."


def no_figure(text: str, *figures: str) -> bool:
    """The safe copy must not carry any of the rejected draft's figures.

    A digit test is not enough: the copies legitimately quote a unit code
    (``CH-03``), which is an identifier, not a price.
    """
    return not any(figure in text for figure in figures)
# the evidence that supports it.
GATED_EVENTS = (
    SSE_EVENT_TOKEN, SSE_EVENT_SOURCES, SSE_EVENT_FACTS, SSE_EVENT_IMAGES, SSE_EVENT_PLACES,
)


def sha(text: str) -> str:
    """Independent oracle — NOT the production ``sha256_hex``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def number_free(text: str) -> bool:
    """A safe copy carries no figure at all, so no cut can invent one."""
    return re.search(r"\d", text) is None


def _chunk(chunk_id: str = "camellia-amenities", **over) -> dict:
    base = {
        "id": chunk_id, "content": CONTENT, "score": 0.91, "doc_id": DOC,
        "title": "Hồ sơ tiện ích Camellia", "section": "Tiện ích",
        "project_key": "camellia", "status": "published",
        "effective_from": START, "effective_to": None,
    }
    base.update(over)
    return base


def _registry_row(chunk: dict, *, project_key: str | None = None) -> dict:
    return {
        "chunk_id": chunk["id"], "registry_content": chunk["content"],
        "doc_id": chunk["doc_id"], "title": chunk["title"], "section": chunk["section"],
        "kind": "project", "project_key": project_key or chunk["project_key"],
        "status": "published", "effective_from": date(2020, 1, 1), "effective_to": None,
    }


def _fact_row(
    fact_id: int,
    *,
    unit_code: str = "CH-03",
    fact_key: str = "price_vnd",
    value: int | None = 2_850_000_000,
    quality: str = "exact",
    trust: str | None = "confirmed",
    range_min: int | None = None,
    range_max: int | None = None,
) -> dict:
    """One raw ``facts`` row, exactly the column set ``build_sql`` projects."""
    return {
        "fact_id": fact_id,
        "subject_key": f"unit:camellia/{unit_code}",
        "policy_key": "htls",
        "campaign_key": CAMPAIGN,
        "fact_key": fact_key,
        "value_num": value,
        "value_text": None,
        "unit": "vnd",
        "quality": quality,
        "range_min": range_min,
        "range_max": range_max,
        "effective_from": START,
        "effective_to": None,
        "source_doc_id": DOC,
        "source_chunk_id": None,
        "trust_level": trust,
        "source_doc_status": "published",
        "source_doc_effective_from": START,
        "source_doc_effective_to": None,
        "source_doc_project_key": "camellia",
        "campaign_project_key": "camellia",
        "campaign_status": "active",
        "campaign_effective_from": START,
        "campaign_effective_to": None,
        "campaign_source_doc_id": DOC,
        "campaign_source_doc_status": "published",
        "campaign_source_doc_effective_from": START,
        "campaign_source_doc_effective_to": None,
        "campaign_source_doc_project_key": "camellia",
    }


def _long_descriptive() -> str:
    # One paragraph on purpose: the bound prefers the LAST break inside the
    # window, so an early paragraph break would make a very short cut correct.
    return (
        "Căn hộ tại Camellia. "
        + "Camellia có vườn cây xanh và lối dạo bộ cho cư dân. " * 8
        + "Thông tin được đối chiếu với hồ sơ tiện ích của dự án. " * 40
        + "Kết luận: dự án có tiện ích đầy đủ."
    )


class _Reranker:
    async def rerank(self, query, chunks):
        return chunks


class _Harness:
    """Seam installation for one real workflow/facade run."""

    def __init__(self, monkeypatch) -> None:
        self.monkeypatch = monkeypatch
        self.events: list[tuple[str, dict]] = []
        self.order: list[str] = []
        self.audits: list[dict] = []
        self.audit_gate: asyncio.Event | None = None
        self.audit_started = 0
        self.audit_completed = 0
        self.verifier_inputs: list[str] = []

    # -- audit: the release latch -----------------------------------------
    async def _audit(self, entry: dict) -> None:
        self.audits.append(copy.deepcopy(entry))
        self.order.append("audit_start")
        self.audit_started += 1
        if self.audit_gate is not None:
            await self.audit_gate.wait()
        self.audit_completed += 1
        self.order.append("audit_done")

    def _on_event(self, event: str, data: dict) -> None:
        self.events.append((event, data))
        self.order.append(f"emit:{event}")

    # -- legs --------------------------------------------------------------
    def install(
        self,
        *,
        query: str,
        draft: str,
        chunks: list[dict] | None = None,
        rows: list[dict] | None = None,
        needs_sql: bool = False,
        fail_stream: bool = False,
    ) -> None:
        rows = rows or []
        raw_chunks = list(chunks) if chunks is not None else [_chunk()]
        records = [_registry_row(c) for c in raw_chunks]

        async def fake_guard(raw):
            return InputGuardResult(clean=raw, rejected=False, degraded=True)

        async def fake_rewrite(clean, history, as_of_iso):
            return RoutedResult(
                rewritten=clean,
                routing={
                    "needs_rag": True, "needs_sql": needs_sql,
                    "structured_path": "spec" if needs_sql else "none", "needs_geo": False,
                },
                sql_spec={"source": "facts"} if needs_sql else None,
                hl_keywords=[], ll_keywords=[], high_stakes=False, as_of=None,
            )

        async def fake_rag(*args):
            return RagLegResult(chunks=raw_chunks)

        async def fake_sql(*args):
            if not needs_sql:
                return SqlLegResult([], {"mode": "none"})
            return SqlLegResult(
                build_fact_evidence(rows, "facts", date.today()),
                {"mode": "spec", "source": "facts"},
            )

        async def fake_stream(merged, history, high_stakes):
            # The real prompt builder runs: _prompt_rag_rows/_prompt_fact_rows
            # are what publication authority is decided against.
            build_messages(merged, history)
            if fail_stream:
                yield draft[: len(draft) // 3]
                raise GenerationProviderError("provider died mid-answer")
            yield draft

        async def observing_guard(answer, facts, sources, routing, meta=None):
            self.verifier_inputs.append(answer)
            return await guard_output(answer, facts, sources, routing, meta=meta)

        async def price_probe(project_key, unit_codes=None, **kwargs):
            return ConfirmedPriceProbe(True, None, frozenset(), False)

        class Connection:
            async def fetch(self, sql_text, ids):
                return records

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

        async def load_record(project_key):
            return ProjectRegistryRecord(
                project_key=project_key, ten_thuong_mai="Camellia", ten_phap_ly=None,
                vi_tri=None, hotline=None, location="Da Nang", geo_center_lat=None,
                geo_center_lng=None, is_hot=False, status="active",
            )

        m = self.monkeypatch

        m.setattr(workflow_module, "guard_input", fake_guard)
        m.setattr(workflow_module, "rewrite_query", fake_rewrite)
        m.setattr(workflow_module, "get_reranker", lambda: _Reranker())
        m.setattr(workflow_module, "run_rag_leg", fake_rag)
        m.setattr(workflow_module, "run_sql_leg", fake_sql)
        m.setattr(workflow_module, "stream_answer", fake_stream)
        m.setattr(workflow_module, "guard_output", observing_guard)
        m.setattr(workflow_module, "write_audit", self._audit)
        m.setattr(workflow_module, "probe_confirmed_price", price_probe)
        m.setattr(
            workflow_module, "search_images", lambda *a, **k: asyncio.sleep(0, result=[])
        )
        m.setattr(merge_module, "get_ro_pool", pool)
        m.setattr(
            "api.application.services.project_config.load_project_registry_record", load_record
        )

    # -- run ---------------------------------------------------------------
    def pipeline(self, facade: str, on_event=None):
        """The facade's emitter. The real core always supplies the request's
        ``on_event``; a direct workflow-level run supplies the recorder."""
        emit = self._on_event if on_event is None else on_event
        return RagQueryPipelineConv(on_event=emit) if facade == "conv" else (
            RagQueryPipeline(on_event=emit)
        )

    def gated(self) -> list[str]:
        return [name for name, _ in self.events if name in GATED_EVENTS]

    def tokens(self) -> str:
        return "".join(d.get("text", "") for name, d in self.events if name == SSE_EVENT_TOKEN)

    def assert_bytes_agree(self, result: dict, expected: str) -> None:
        """The one invariant: token == result == hash, byte for byte."""
        expected_bytes = expected.encode("utf-8")
        assert self.tokens().encode("utf-8") == expected_bytes
        assert result["answer"].encode("utf-8") == expected_bytes
        assert self.audits[-1]["answer_hash"] == sha(expected)
        assert self.audits[-1]["answer_hash"] == hashlib.sha256(expected_bytes).hexdigest()


async def _run_async(harness: _Harness, *, facade: str, visible_limit, **kwargs) -> dict:
    return await harness.pipeline(facade).run(
        kwargs["query"],
        f"p05-{facade}-{visible_limit}",
        None,
        [],
        project_context=PROJECT,
        visible_limit=visible_limit,
    )


def _run(harness: _Harness, **kwargs) -> dict:
    facade = kwargs.pop("facade", "direct")
    visible_limit = kwargs.pop("visible_limit", None)
    return asyncio.run(
        _run_async(harness, facade=facade, visible_limit=visible_limit, **kwargs)
    )


# ---------------------------------------------------------------------------
# 1. Publish: hash/token/result agreement, and nothing escapes before the audit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("facade", ["direct", "conv"])
def test_workflow_publish_hashes_final_bytes_before_any_emit(monkeypatch, facade):
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia tiện ích", draft=DESCRIPTIVE)

    async def go():
        harness.audit_gate = asyncio.Event()
        task = asyncio.create_task(
            _run_async(harness, facade=facade, visible_limit=None,
                       query="Camellia tiện ích", draft=DESCRIPTIVE)
        )
        for _ in range(1000):
            if harness.audit_started:
                break
            await asyncio.sleep(0)
        # The audit write is attempted and parked: nothing may have shipped yet.
        assert harness.audit_started == 1
        assert harness.gated() == [], harness.gated()
        harness.audit_gate.set()
        return await task

    result = asyncio.run(go())

    # Post-gate: the audit settled first, then every body/evidence event.
    first_gated = min(
        i for i, step in enumerate(harness.order)
        if step.startswith("emit:") and step.split(":", 1)[1] in GATED_EVENTS
    )
    assert harness.order.index("audit_done") < first_gated
    harness.assert_bytes_agree(result, DESCRIPTIVE)
    assert result["confidence"] == "MEDIUM" and result["requires_review"] is True
    sources = next(d for n, d in harness.events if n == SSE_EVENT_SOURCES)["sources"]
    assert [s["doc_id"] for s in sources] == [DOC]
    assert result["sources"] == sources
    # The workflow never persists; the audit trace is the join key for that.
    assert harness.audits[-1]["trace_id"] == result["trace_id"]


def test_safe_fallback_hashes_copy_not_rejected_draft(monkeypatch):
    harness = _Harness(monkeypatch)
    draft = "Căn CH-03 có giá 99 tỷ theo bảng giá."
    harness.install(query="giá căn CH-03", draft=draft, needs_sql=True, rows=[])

    result = _run(harness, query="giá căn CH-03", draft=draft, needs_sql=True, rows=[])

    assert draft not in result["answer"]
    assert no_figure(result["answer"], "99 tỷ", "bảng giá") and result["answer"].strip()
    harness.assert_bytes_agree(result, result["answer"])
    # The rejected draft's own verdict stays fail — the final-visible recheck
    # never rewrites it into a pass.
    assert harness.audits[-1]["guard_verdicts"]["numeric_grounding"] == "fail"
    # A figure-free copy carries none of the rejected draft's evidence rights.
    assert result["sources"] == [] and result["facts"] == [] and result["images"] == []
    assert harness.gated() == [SSE_EVENT_TOKEN]


def test_partial_generation_never_reaches_publication_gate(monkeypatch):
    harness = _Harness(monkeypatch)
    draft = "Camellia có vườn cây xanh và lối dạo bộ rộng rãi."
    harness.install(query="Camellia tiện ích", draft=draft, fail_stream=True)

    with pytest.raises(GenerationProviderError):
        _run(harness, query="Camellia tiện ích", draft=draft, fail_stream=True)

    assert harness.tokens() == ""
    assert harness.gated() == []
    assert harness.audits == []


# ---------------------------------------------------------------------------
# 2. Bounded delivery: the bound is applied before check / hash / token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("facade", ["direct", "conv"])
def test_bounded_descriptive_answer_is_identical_across_facades(monkeypatch, facade):
    draft = _long_descriptive()
    assert len(draft) > 2000
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia tiện ích", draft=draft)

    result = _run(harness, facade=facade, visible_limit=2000,
                  query="Camellia tiện ích", draft=draft)

    expected = harness.tokens()
    assert len(expected) <= 2000
    assert expected.startswith("Căn hộ tại Camellia.")
    # The suffix is canonicalized away, and the surviving text is a fixed point:
    # another canonicalize+bound pass cannot change the audited bytes.
    assert "[truncated]" not in expected
    assert len(expected) > 1000  # a cut, not a decapitation
    harness.assert_bytes_agree(result, expected)
    # The bound preserved the selected evidence.
    assert result["sources"] and result["facts"] is not None


def test_visible_limit_none_preserves_the_whole_answer(monkeypatch):
    draft = _long_descriptive()
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia tiện ích", draft=draft)

    result = _run(harness, visible_limit=None, query="Camellia tiện ích", draft=draft)

    assert result["answer"] == draft
    harness.assert_bytes_agree(result, draft)


@pytest.mark.parametrize("bad", [0, 1, True, False, -5, 3.5, "2000"])
def test_budget_that_cannot_hold_a_safe_copy_is_refused(monkeypatch, bad):
    """A budget below the longest STATIC refusal copy is rejected up front.

    `output_guard` replaces a refused answer with a fixed, figure-free sentence.
    A budget shorter than the longest such sentence would have to ship either a
    mutilated safety message or an over-budget body event, so the turn is
    rejected in `guard` — before retrieval, generation, or any emission.
    `True`/`False` are listed because `bool` subclasses `int`: without an
    explicit check a truthy flag becomes a one-character budget.
    """
    from api.application.pipelines.workflow import MIN_VISIBLE_CHARS, QueryRejected

    draft = "Căn hộ tại Camellia. " * 100
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia tiện ích", draft=draft)

    with pytest.raises(QueryRejected) as excinfo:
        _run(harness, visible_limit=bad, query="Camellia tiện ích", draft=draft)

    assert str(MIN_VISIBLE_CHARS) in str(excinfo.value)
    # Nothing was published, and the refusal is on the audit record.
    assert harness.gated() == []
    assert harness.audits[-1]["guard_verdicts"] == {
        "L1": "reject", "reason": "invalid_visible_limit",
    }


def test_floor_budget_is_accepted_and_never_exceeded(monkeypatch):
    """The smallest accepted budget must be able to hold the fallback itself."""
    from api.application.pipelines.workflow import (
        LOW_GROUNDING_MESSAGE, MIN_VISIBLE_CHARS, VERIFICATION_FALLBACK_MESSAGE,
    )

    assert MIN_VISIBLE_CHARS >= len(LOW_GROUNDING_MESSAGE)
    assert MIN_VISIBLE_CHARS >= len(VERIFICATION_FALLBACK_MESSAGE)

    # An oversized QUALIFIED draft at exactly the floor: the only substitutable
    # copy fits, so the released body is within budget rather than over it.
    draft = (
        "Căn CH-03 của Camellia có giá chính thức 2.850.000.000 đồng theo bảng giá. "
        + "Giá đã bao gồm VAT và phí quản lý, liên hệ chủ đầu tư để được tư vấn. " * 40
    )
    assert len(draft) > MIN_VISIBLE_CHARS
    rows = [_fact_row(1)]
    harness = _Harness(monkeypatch)
    harness.install(query="Giá căn CH-03", draft=draft, needs_sql=True, rows=rows)

    result = _run(harness, visible_limit=MIN_VISIBLE_CHARS, query="Giá căn CH-03",
                  draft=draft, needs_sql=True, rows=rows)

    assert len(result["answer"]) <= MIN_VISIBLE_CHARS
    assert result["answer"] == LOW_GROUNDING_MESSAGE
    no_figure(result["answer"], "2,85", "2.850", "VAT")
    harness.assert_bytes_agree(result, result["answer"])
    # The evidence rights of the refused draft were dropped, not shipped.
    assert result["sources"] == [] and result["facts"] == []


def test_multi_unit_limitation_copy_is_never_truncated(monkeypatch):
    """A per-unit price copy is built dynamically and shipped WHOLE.

    Truncating it would silently drop a unit the user actually asked about, so
    the bound is never applied to a safety copy: the complete string is what
    gets emitted, and its bytes are what the audit hashes.
    """
    draft = "Giá căn CH-03, CH-13A và CH-07 tại Camellia. " * 40
    harness = _Harness(monkeypatch)
    harness.install(query="Giá căn CH-03 CH-13A CH-07", draft=draft, needs_sql=True, rows=[])

    result = _run(harness, visible_limit=2000,
                  query="Giá căn CH-03 CH-13A CH-07", draft=draft, needs_sql=True, rows=[])

    for unit in ("CH-03", "CH-13A", "CH-07"):
        assert unit in result["answer"], unit
    harness.assert_bytes_agree(result, result["answer"])
    assert harness.audits[-1]["guard_verdicts"]["price_limitation"] == (
        "no_confirmed_unit_price"
    )


def test_safety_copy_too_long_for_the_budget_is_refused_unemitted(monkeypatch):
    """An over-budget safety copy is refused, never truncated and never shipped.

    `MIN_VISIBLE_CHARS` only bounds the STATIC copies, so a multi-unit price
    copy can outgrow the budget. The GA watchdog sees the answer only after
    `run` returns, so the turn must already be refused here — before any body,
    token, or evidence event is released.
    """
    from api.application.pipelines.workflow import MIN_VISIBLE_CHARS, QueryRejected

    draft = "Giá căn CH-03, CH-13A và CH-07 tại Camellia. " * 40
    harness = _Harness(monkeypatch)
    harness.install(query="Giá căn CH-03 CH-13A CH-07", draft=draft, needs_sql=True, rows=[])

    with pytest.raises(QueryRejected):
        _run(harness, visible_limit=MIN_VISIBLE_CHARS,
             query="Giá căn CH-03 CH-13A CH-07", draft=draft, needs_sql=True, rows=[])

    assert harness.gated() == []
    verdicts = harness.audits[-1]["guard_verdicts"]
    assert verdicts["L1"] == "reject"
    assert verdicts["reason"] == "safe_copy_exceeds_visible_limit"
    # The completed output-guard decision is merged in, never overwritten.
    assert verdicts["price_limitation"] == "no_confirmed_unit_price"


def test_clarification_copy_respects_the_floor_budget(monkeypatch):
    """The unresolved branch builds its copy dynamically, before the pipeline.

    Its length grows with `clarification_candidates()`, so the static floor is
    not a substitute for bounding it. This pins that branch: an oversized
    candidate list is bounded like any other answer rather than shipped whole.
    """
    from api.application.pipelines.workflow import MIN_VISIBLE_CHARS

    harness = _Harness(monkeypatch)
    harness.install(query="tien ich noi bo", draft=DESCRIPTIVE)
    history = _FakeHistory()

    async def many_candidates():
        return [(f"camellia-d{i}", f"Dự án Camellia Residence {i}") for i in range(40)]

    core = _build_core(harness, history)
    core.catalogue_loader = many_candidates

    outcome = asyncio.run(core.run(GlobalAssistantInput(
        query="tien ich noi bo", session_id="p05-clarify", identity_key="anon:t",
        max_visible_chars=MIN_VISIBLE_CHARS,
    )))

    assert outcome.resolution_status != "resolved"
    assert 0 < len(outcome.answer) <= MIN_VISIBLE_CHARS
    assert history.appended[0]["assistant_content"] == outcome.answer


def test_bound_cannot_drop_financial_safety_qualification(monkeypatch):
    """An oversized qualified financial answer becomes the number-free copy.

    The interface exposes no span contract for "ước tính" / VAT / specialist
    qualification, so a CHANGED qualified answer is never published truncated.
    """
    draft = (
        "Căn CH-03 của Camellia có giá chính thức 2.850.000.000 đồng theo bảng giá. "
        + "Giá đã bao gồm VAT và phí quản lý, liên hệ chủ đầu tư để được tư vấn. " * 60
    )
    assert len(draft) > 2000
    rows = [_fact_row(1)]
    harness = _Harness(monkeypatch)
    harness.install(query="Giá căn CH-03", draft=draft, needs_sql=True, rows=rows)

    result = _run(harness, visible_limit=2000, query="Giá căn CH-03", draft=draft,
                  needs_sql=True, rows=rows)

    assert no_figure(result["answer"], "2,85", "2.850", "VAT")
    harness.assert_bytes_agree(result, result["answer"])
    final_visible = harness.audits[-1]["guard_verdicts"]["final_visible"]
    assert final_visible["bounded"] is True and final_visible["shortened_qualified"] is True
    assert harness.gated() == [SSE_EVENT_TOKEN]


def test_fitting_qualified_financial_answer_still_publishes(monkeypatch):
    """The same qualified answer that FITS the budget keeps its figures."""
    draft = "Căn CH-03 của Camellia có giá chính thức 2.850.000.000 đồng, tức 2,85 tỷ."
    rows = [_fact_row(1)]
    harness = _Harness(monkeypatch)
    harness.install(query="Giá căn CH-03", draft=draft, needs_sql=True, rows=rows)

    result = _run(harness, visible_limit=2000, query="Giá căn CH-03", draft=draft,
                  needs_sql=True, rows=rows)

    harness.assert_bytes_agree(result, draft)
    assert result["facts"]
    assert harness.gated() != [SSE_EVENT_TOKEN]  # evidence really was published


def test_bounding_can_never_split_a_supported_figure(monkeypatch):
    """A bound cut through a number cannot produce a plausible-looking prefix."""
    draft = (
        "Căn CH-03 của Camellia có giá chính thức 2.850.000.000 đồng theo bảng giá. "
        + "Chi tiết liên hệ chủ đầu tư để được tư vấn. " * 60
    )
    rows = [_fact_row(1)]
    harness = _Harness(monkeypatch)
    harness.install(query="Giá căn CH-03", draft=draft, needs_sql=True, rows=rows)

    result = _run(harness, visible_limit=400, query="Giá căn CH-03", draft=draft,
                  needs_sql=True, rows=rows)

    assert no_figure(result["answer"], "2,85", "2.850", "đồng")
    harness.assert_bytes_agree(result, result["answer"])


# ---------------------------------------------------------------------------
# 3. Canonicalization happens BEFORE the final check and the hash
# ---------------------------------------------------------------------------


def test_postprocessing_cannot_change_audited_bytes(monkeypatch):
    draft = "Căn hộ hai phòng ngủ [chunk_abc123] của Camellia.<br>Vườn cây xanh. Kết thúc."
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia loại căn hộ", draft=draft)

    result = _run(harness, query="Camellia loại căn hộ", draft=draft)

    expected = "Căn hộ hai phòng ngủ của Camellia.\nVườn cây xanh. Kết thúc."
    assert draft != expected
    harness.assert_bytes_agree(result, expected)
    assert "[chunk_abc123]" not in result["answer"] and "<br>" not in result["answer"]


def test_orphan_citation_is_blocked_before_sanitation_can_hide_it(monkeypatch):
    """`[fe-999]` names no selected fact. The published copy must not contain a
    figure, whatever the verifier said about the original draft."""
    draft = "Giá 2 tỷ [fe-999] theo Bảng giá"
    rows = [_fact_row(1)]
    harness = _Harness(monkeypatch)
    harness.install(query="Giá căn CH-03", draft=draft, needs_sql=True, rows=rows)

    result = _run(harness, query="Giá căn CH-03", draft=draft, needs_sql=True, rows=rows)

    assert no_figure(result["answer"], "2 tỷ", "fe-999")
    assert draft not in result["answer"]
    assert harness.audits[-1]["guard_verdicts"]["citation_grounding"]["status"] == "fail"


def test_numeric_citation_and_plain_prose_survive(monkeypatch):
    draft = "Chính sách [1] hiện hành gồm chiết khấu cho khách hàng thân thiết."
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia chính sách", draft=draft)

    result = _run(harness, query="Camellia chính sách", draft=draft)

    harness.assert_bytes_agree(result, draft)


# ---------------------------------------------------------------------------
# 4. Selected provenance (D6) and the GA core contract
# ---------------------------------------------------------------------------


def test_selected_prompt_ids_are_not_retrieval_audit_ids(monkeypatch):
    """Retrieval carries a dropped candidate; only the serialized prompt row is
    publication authority, and the verdict JSON records what was serialized."""
    kept = _chunk("camellia-amenities")
    dropped = _chunk("camellia-foreign", project_key="soleil", doc_id="soleil-doc",
                     title="Tài liệu Soleil", section="Khác")
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia tiện ích", draft=DESCRIPTIVE, chunks=[kept, dropped])

    result = _run(harness, query="Camellia tiện ích", draft=DESCRIPTIVE,
                  chunks=[kept, dropped])

    verdicts = harness.audits[-1]["guard_verdicts"]
    # Retrieval-only audit column keeps the dropped candidate...
    assert set(harness.audits[-1]["chunk_ids"]) == {"camellia-amenities", "camellia-foreign"}
    # ...while the serialized-prompt verdict carries only the row that reached
    # the prompt, and only that row's document is published.
    assert verdicts["selected_prompt_chunk_ids"] == ["camellia-amenities"]
    assert [s["doc_id"] for s in result["sources"]] == [DOC]


class _FakeHistory:
    def __init__(self, messages=None) -> None:
        self.messages = list(messages or [])
        self.appended: list[dict] = []

    async def global_session(self, *, session_id, device_id=None, identity_key=None):
        return None

    async def global_messages(self, *, session_id, device_id=None, identity_key=None):
        return list(self.messages)

    async def persist_global_turn(self, **kwargs):
        self.appended.append(kwargs)
        return True


def _build_core(harness: _Harness, history, *, pipeline=None):
    async def catalogue():
        return [("camellia", "Camellia"), ("soleil", "Soleil")]

    def factory(on_event=None):
        return (pipeline(on_event=on_event) if pipeline is not None
                else harness.pipeline("conv", on_event=on_event))

    return GlobalAssistantService(
        history=history, pipeline_factory=factory, catalogue_loader=catalogue,
    )


@pytest.mark.parametrize("facade", ["direct", "conv"])
def test_ga_persists_and_returns_already_bounded_pipeline_answer(monkeypatch, facade):
    draft = _long_descriptive()
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia tiện ích", draft=draft)
    history = _FakeHistory()
    core = _build_core(harness, history, pipeline=lambda on_event=None: harness.pipeline(facade))

    outcome = asyncio.run(core.run(GlobalAssistantInput(
        query="Camellia tiện ích", session_id="p05-ga", identity_key="anon:t",
        max_visible_chars=2000,
    )))

    assert len(outcome.answer) <= 2000
    harness.assert_bytes_agree({"answer": outcome.answer}, harness.tokens())
    assert len(history.appended) == 1
    persisted = history.appended[0]["assistant_content"]
    assert persisted.encode("utf-8") == outcome.answer.encode("utf-8")
    # Trace correlation: outcome -> persisted meta -> audit row.
    assert outcome.trace_id
    assert history.appended[0]["assistant_meta"]["ga_outcome"]["trace_id"] == outcome.trace_id
    assert harness.audits[-1]["trace_id"] == outcome.trace_id


def test_ga_rejects_an_audited_answer_that_violates_the_bound(monkeypatch):
    """A nonconforming adapter is refused BEFORE any core persistence."""
    harness = _Harness(monkeypatch)
    draft = "Căn hộ tại Camellia. " + "Vườn cây xanh và lối dạo bộ. " * 200
    harness.install(query="Camellia tiện ích", draft=draft)
    history = _FakeHistory()

    class _OversizedFacade:
        """Ignores the budget entirely — the exact GA-08 shape P0-05 forbids."""

        def __init__(self, on_event=None) -> None:
            self.on_event = on_event

        async def run(self, query, session_id=None, as_of=None, history=None, **kwargs):
            return {"answer": draft, "trace_id": "t-oversized", "sources": []}

    core = _build_core(harness, history, pipeline=_OversizedFacade)

    with pytest.raises(GlobalAssistantAnswerIntegrityError):
        asyncio.run(core.run(GlobalAssistantInput(
            query="Camellia tiện ích", session_id="p05-ga-bad", identity_key="anon:t",
            max_visible_chars=2000,
        )))

    assert history.appended == []


def test_ga_replay_keeps_original_visible_bytes_without_new_execution(monkeypatch):
    harness = _Harness(monkeypatch)
    draft = _long_descriptive()
    harness.install(query="Camellia tiện ích", draft=draft)
    history = _FakeHistory()
    core = _build_core(harness, history)
    request = dict(query="Camellia tiện ích", session_id="p05-replay",
                   identity_key="anon:t", max_visible_chars=2000,
                   source_event_key="evt-1")

    first = asyncio.run(core.run(GlobalAssistantInput(**request)))
    audits_after_first = len(harness.audits)
    stored = copy.deepcopy(history.appended[0]["assistant_meta"]["ga_outcome"])
    history.messages = [
        type("M", (), {
            "role": "assistant", "project_key": "camellia",
            "meta": {"_source_event_key": "evt-1", "ga_outcome": stored},
            "content": stored["answer"],
        })()
    ]

    second = asyncio.run(core.run(GlobalAssistantInput(**request)))

    assert second.replayed is True
    assert second.answer.encode("utf-8") == first.answer.encode("utf-8")
    assert len(history.appended) == 1  # no second history write
    assert len(harness.audits) == audits_after_first  # no new audit row


def test_unresolvable_budget_is_refused_before_publication(monkeypatch):
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia tiện ích", draft=DESCRIPTIVE)
    core = _build_core(harness, _FakeHistory())

    with pytest.raises(ValueError):
        asyncio.run(core.run(GlobalAssistantInput(
            query="Camellia tiện ích", session_id="p05-bad-limit", identity_key="anon:t",
            max_visible_chars=0,
        )))

    assert harness.gated() == [] and harness.audits == []
