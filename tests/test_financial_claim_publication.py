"""P0-03C: every money/rate/payment figure is authorized CLAIM BY CLAIM.

The matcher in ``api/application/pipelines/workflow.py`` only lets a figure
publish when one selected prompt row

* carries a ``fact_id`` byte-identical to the SQL leg row it was serialized from
  (so a fabricated or id-less prompt row authorizes nothing),
* is ``exact`` + ``confirmed`` (or a labelled ``range`` + ``estimate``) with a
import pytest

  unit matching the figure,
* names the figure's OWN semantic slot — a price never licenses a same-valued
  deposit/payment/rate,
* resolves to one subject and one policy (the clause first, the query as
  fallback), and
* carries a current fact interval, published source document, active
  same-project campaign and published campaign document, all checked against
  ``date.today()``.

Everything here runs the REAL ``RagQueryWorkflow`` with the REAL
``build_fact_evidence``, ``merge_context``, ``build_messages`` and
``guard_output``; only the LLM, the DB pool and the absence probe are stubbed.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

import api.application.pipelines.workflow as workflow_module
import api.application.services.merge as merge_module
from api.application.pipelines.workflow import (
    LOW_GROUNDING_MESSAGE,
    NO_CONFIRMED_PROJECT_PRICE,
    NO_CONFIRMED_UNIT_PRICE,
    PRICE_LOOKUP_UNCERTAIN,
    RagQueryWorkflow,
)
from api.application.services.generate import build_messages
from api.application.services.project_context import ProjectContext
from api.application.services.rag_leg import RagLegResult
from api.application.services.sql_leg import ConfirmedPriceProbe, SqlLegResult, build_fact_evidence
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import guard_output
from api.domain.services.rewrite import RoutedResult
from api.domain.value_objects.constants import SSE_EVENT_FACTS, SSE_EVENT_IMAGES, SSE_EVENT_TOKEN

PROJECT = ProjectContext("camellia", "normal", (("camellia", "Camellia"),))
START = (date.today() - timedelta(days=30)).isoformat()
TODAY = date.today()
DOC = "price-camellia-2026q3"
CAMPAIGN = "camellia-2026q3"
CONTENT = "Camellia có vườn cây xanh và lối dạo bộ."


class _Reranker:
    async def rerank(self, query, chunks):
        return chunks


def _chunk():
    return {
        "id": "camellia-amenities", "content": CONTENT, "score": 0.91,
        "doc_id": DOC, "title": "Bảng giá Camellia", "section": "Giá bán",
        "project_key": "camellia", "status": "published",
        "effective_from": START, "effective_to": None,
    }


def _fact_row(
    fact_id: int,
    *,
    unit_code: str = "CH-03",
    policy_key: str | None = "htls",
    fact_key: str = "price_vnd",
    value: int | None = 2_850_000_000,
    unit: str | None = "vnd",
    quality: str = "exact",
    trust: str | None = "confirmed",
    range_min: int | None = None,
    range_max: int | None = None,
    effective_to: str | None = None,
    source_doc_status: str | None = "published",
    source_doc_effective_to: str | None = None,
    campaign_status: str | None = "active",
    campaign_effective_to: str | None = None,
    campaign_source_doc_status: str | None = "published",
) -> dict:
    """One raw ``facts`` row, exactly the column set ``build_sql`` projects."""
    return {
        "fact_id": fact_id,
        "subject_key": f"unit:camellia/{unit_code}",
        "policy_key": policy_key,
        "campaign_key": CAMPAIGN,
        "fact_key": fact_key,
        "value_num": value,
        "value_text": None,
        "unit": unit,
        "quality": quality,
        "range_min": range_min,
        "range_max": range_max,
        "effective_from": START,
        "effective_to": effective_to,
        "source_doc_id": DOC,
        "source_chunk_id": None,
        "trust_level": trust,
        "source_doc_status": source_doc_status,
        "source_doc_effective_from": START,
        "source_doc_effective_to": source_doc_effective_to,
        "source_doc_project_key": "camellia",
        "campaign_project_key": "camellia",
        "campaign_status": campaign_status,
        "campaign_effective_from": START,
        "campaign_effective_to": campaign_effective_to,
        "campaign_source_doc_id": DOC,
        "campaign_source_doc_status": campaign_source_doc_status,
        "campaign_source_doc_effective_from": START,
        "campaign_source_doc_effective_to": None,
        "campaign_source_doc_project_key": "camellia",
    }


def _run(
    monkeypatch,
    query: str,
    answer: str,
    rows: list[dict] | None = None,
    *,
    probe: ConfirmedPriceProbe | None = None,
    needs_sql: bool = True,
    sql_error: bool = False,
    audits: list[dict] | None = None,
):
    """Drive the real workflow over the given raw fact rows."""
    events: list[tuple[str, dict]] = []
    rows = rows or []

    async def fake_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        return RoutedResult(
            rewritten=clean,
            routing={
                "needs_rag": True, "needs_sql": needs_sql,
                "structured_path": "nl2sql" if needs_sql else "none", "needs_geo": False,
            },
            sql_spec={"source": "facts"} if needs_sql else None,
            hl_keywords=[], ll_keywords=[], high_stakes=False, as_of=None,
        )

    async def fake_rag(*args):
        return RagLegResult(chunks=[_chunk()])

    async def fake_sql(*args):
        if sql_error:
            return SqlLegResult([], {"mode": "spec", "error": "lookup failed"}, degraded=True)
        return SqlLegResult(
            build_fact_evidence(rows, "facts", TODAY), {"mode": "spec", "source": "facts"}
        )

    async def fake_stream(merged, history, high_stakes):
        build_messages(merged, history)
        yield answer

    async def fake_audit(entry):
        if audits is not None:
            audits.append(entry)
        return None

    async def price_probe(project_key, unit_codes=None, **kwargs):
        return probe if probe is not None else ConfirmedPriceProbe(True, None, frozenset(), False)

    class RegistryConnection:
        async def fetch(self, sql, ids):
            return [{
                "chunk_id": _chunk()["id"], "registry_content": CONTENT, "doc_id": DOC,
                "title": "Bảng giá Camellia", "section": "Giá bán", "kind": "project",
                "project_key": "camellia", "status": "published",
                "effective_from": date(2020, 1, 1), "effective_to": None,
            }]

        async def fetchrow(self, *args, **kwargs):
            return None

    class RegistryAcquire:
        async def __aenter__(self):
            return RegistryConnection()

        async def __aexit__(self, *args):
            return None

    class RegistryPool:
        def acquire(self):
            return RegistryAcquire()

    async def fake_pool():
        return RegistryPool()

    monkeypatch.setattr(workflow_module, "guard_input", fake_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _Reranker())
    monkeypatch.setattr(workflow_module, "run_rag_leg", fake_rag)
    monkeypatch.setattr(workflow_module, "run_sql_leg", fake_sql)
    monkeypatch.setattr(workflow_module, "stream_answer", fake_stream)
    monkeypatch.setattr(workflow_module, "guard_output", guard_output)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)
    monkeypatch.setattr(workflow_module, "probe_confirmed_price", price_probe)
    # The restated "2,85 tỷ" rides the same row: every span in the clause IS it.
    monkeypatch.setattr(merge_module, "get_ro_pool", fake_pool)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: events.append((e, d)))
        return await wf.run(query=query, session_id=None, history=[], project_context=PROJECT)

    result = asyncio.run(go())
    tokens = "".join(d.get("text", "") for e, d in events if e == SSE_EVENT_TOKEN)
    return result, tokens, events


def _frames(events):
    return {name for name, _ in events}


# --- A: a proven exact price is publishable ---------------------------------


def test_confirmed_exact_unit_price_publishes(monkeypatch):
    answer = "Căn CH-03 có giá 2.850.000.000 đồng, tức 2,85 tỷ."
    result, tokens, events = _run(monkeypatch, "Giá căn CH-03?", answer, [_fact_row(1)])
    assert tokens == answer
    assert result["answer"] == answer
    assert result["confidence"] in {"HIGH", "MEDIUM"}
    # requires_review stays the real guard's own call about the draft; the
    # claim matcher neither forced it up nor replaced the answer.
    # A published, bound answer also publishes its fact card: the row is proven.
    assert [fact["fe_id"] for fact in result["facts"]] == ["fe-001"]


def test_price_for_another_unit_is_not_grounded_by_this_row(monkeypatch):
    answer = "Căn CH-11 có giá 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-11?", answer, [_fact_row(1, unit_code="CH-03")],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    # The claim names CH-11 but the only proven row is CH-03's: unbound.
    assert answer not in tokens
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


# --- C: every claim binds its OWN subject / policy / campaign ---------------


def test_two_units_in_one_answer_each_bind_their_own_subject(monkeypatch):
    answer = "Căn CH-03 có giá 2.850.000.000 đồng. Căn CH-11 có giá 3.100.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "So sánh giá CH-03 và CH-11", answer,
        [_fact_row(1, unit_code="CH-03"), _fact_row(2, unit_code="CH-11", value=3_100_000_000)],
    )
    assert tokens == answer
    assert result["answer"] == answer


def test_swapped_prices_between_two_units_are_rejected(monkeypatch):
    # Each clause names the wrong unit: neither claim is its row's claim.
    answer = "Căn CH-03 có giá 3.100.000.000 đồng. Căn CH-11 có giá 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "So sánh giá CH-03 và CH-11", answer,
        [_fact_row(1, unit_code="CH-03"), _fact_row(2, unit_code="CH-11", value=3_100_000_000)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03", "CH-11"}), True),
    )
    assert answer not in tokens
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN


def test_ambiguous_policy_is_not_resolved_silently(monkeypatch):
    answer = "Căn CH-03 có giá 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer,
        [_fact_row(1, policy_key="htls"), _fact_row(2, policy_key="bank", value=2_850_000_000)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    # Two candidate policies and the clause names neither: unresolved, not guessed.
    assert answer not in tokens
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN


@pytest.mark.parametrize(
    "expiry",
    [
        {"source_doc_effective_to": TODAY.isoformat()},
        {"campaign_effective_to": TODAY.isoformat()},
        {"source_doc_status": "draft"},
        {"campaign_status": "ended"},
        {"campaign_source_doc_status": "draft"},
    ],
)
def test_expired_or_unpublished_lineage_blocks_the_figure(monkeypatch, expiry):
    answer = "Căn CH-03 có giá 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [_fact_row(1, **expiry)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert answer not in tokens
    # Lineage is gone, so we can neither publish the price nor claim absence.
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_expired_fact_row_is_a_prompt_integrity_failure(monkeypatch):
    """An expired SELECTED row is broken provenance, not a missing price."""
    answer = "Căn CH-03 có giá 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer,
        [_fact_row(1, effective_to=TODAY.isoformat())],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert answer not in tokens
    assert result["answer"] == LOW_GROUNDING_MESSAGE
    assert result["confidence"] == "LOW"


# --- D: a labelled estimate range publishes, reviewed -----------------------


def test_labelled_estimate_range_publishes_as_review_only(monkeypatch):
    answer = "Căn CH-03 có giá ước tính từ 1.980.000.000 đồng đến 2.640.000.000 đồng."
    audits: list[dict] = []
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer,
        [_fact_row(1, value=None, quality="range", trust="estimate",
                   range_min=1_980_000_000, range_max=2_640_000_000)],
        audits=audits,
    )
    assert tokens == answer
    assert result["answer"] == answer
    # Authenticated figure, uncheckable verdict: reviewed, confidence untouched.
    assert result["requires_review"] is True
    # The audit keeps the VERIFIER's own view verbatim. A bound figure does not
    # rewrite what the verifier saw: these spans are still orphans to it, and
    # the audit must say so rather than claiming a numeric pass we never got.
    guard_verdicts = audits[-1]["guard_verdicts"]
    assert guard_verdicts["numeric_grounding"] == "fail"
    assert guard_verdicts["orphan_numbers"]


def test_unlabelled_estimate_range_never_publishes(monkeypatch):
    answer = "Căn CH-03 có giá từ 1.980.000.000 đồng đến 2.640.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer,
        [_fact_row(1, value=None, quality="range", trust="estimate",
                   range_min=1_980_000_000, range_max=2_640_000_000)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    # The bounds are real, but an unlabelled range reads as a settled price.
    assert answer not in tokens
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN
    assert result["requires_review"] is True


# --- E: a price never grounds a rate / deposit / payment --------------------


def test_same_valued_price_does_not_ground_a_deposit(monkeypatch):
    answer = "Căn CH-03 cần đặt cọc 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [_fact_row(1)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert answer not in tokens
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN


def test_same_valued_price_does_not_ground_an_interest_rate(monkeypatch):
    answer = "Lãi suất của phương án này là 2.850.000.000%."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [_fact_row(1)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert answer not in tokens
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN


def test_deposit_grounds_from_its_own_fact_key(monkeypatch):
    answer = "Căn CH-03 cần đặt cọc 857.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer,
        [_fact_row(1), _fact_row(2, fact_key="required_down_payment_vnd", value=857_000_000)],
    )
    assert tokens == answer
    assert result["answer"] == answer


# --- F: absence is only ever claimed from a COMPLETED scoped probe ----------


def test_absent_unit_reports_one_sentence_per_missing_code(monkeypatch):
    # CH-3 normalizes to CH-03; the probe answers on the canonical code.
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-3?", "Căn CH-03 có giá 2.850.000.000 đồng.", [],
    )
    assert result["answer"] == NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-03")
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    assert tokens == result["answer"]


def test_two_absent_units_are_reported_in_query_order(monkeypatch):
    result, tokens, _ = _run(
        monkeypatch, "So sánh giá CH-13A và CH-11",
        "CH-13A giá 2 tỷ, CH-11 giá 3 tỷ.", [],
    )
    assert result["answer"] == " ".join([
        NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-13A"),
        NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-11"),
    ])
    assert tokens == result["answer"]


def test_project_wide_absence_uses_the_project_sentence(monkeypatch):
    # No unit is named, so there is no per-unit sentence to emit.
    result, _, _ = _run(
        monkeypatch, "Giá bán chung dự án?", "Giá bán dự án là 2.850.000.000 đồng.", [],
        probe=ConfirmedPriceProbe(True, None, frozenset(), False),
    )
    assert result["answer"] == NO_CONFIRMED_PROJECT_PRICE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_incomplete_probe_reports_uncertainty_not_absence(monkeypatch):
    result, _, _ = _run(
        monkeypatch, "Giá căn CH-03?", "Căn CH-03 có giá 2.850.000.000 đồng.", [],
        probe=ConfirmedPriceProbe(False, "timeout", frozenset(), False),
    )
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN
    assert result["confidence"] == "LOW"


def test_failed_sql_lookup_reports_uncertainty(monkeypatch):
    result, _, _ = _run(
        monkeypatch, "Giá căn CH-03?", "Căn CH-03 có giá 2.850.000.000 đồng.", [],
        sql_error=True,
    )
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN


def test_unselected_but_existing_price_is_uncertain_not_absent(monkeypatch):
    """The probe found a confirmed price in the project, but it was not selected.

    Saying "no confirmed price for CH-11" would be a false absence, so the turn
    reports uncertainty instead.
    """
    result, _, _ = _run(
        monkeypatch, "Giá căn CH-11?", "Căn CH-11 có giá 2.850.000.000 đồng.",
        [_fact_row(1, unit_code="CH-03")],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN
    assert result["answer"] != NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-11")


# --- G: unconfirmed, offer-shaped and fallback rows authorize nothing -------


def test_estimate_trust_row_never_quotes_a_single_price(monkeypatch):
    answer = "Căn CH-03 có giá 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [_fact_row(1, trust="estimate")],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert answer not in tokens
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN


def test_approx_quality_row_never_quotes_a_single_price(monkeypatch):
    answer = "Căn CH-03 có giá 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [_fact_row(1, quality="approx")],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert answer not in tokens
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN


def test_null_trust_row_without_lineage_authorizes_nothing(monkeypatch):
    answer = "Căn CH-03 có giá 2.850.000.000 đồng."
    row = _fact_row(1, trust=None, campaign_status=None, source_doc_status=None)
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [row],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert answer not in tokens
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN


def test_ambiguous_fact_id_in_the_leg_authorizes_nothing(monkeypatch):
    """A fact id that identifies TWO leg rows proves nothing.

    ``merge`` re-reads the leg output as ``_fact_evidence``, and a selected
    prompt row is authority only when its ``fact_id`` matches exactly one leg
    row on every field a claim depends on. A duplicated id is ambiguous, so
    neither row may license a figure.
    """
    answer = "Căn CH-03 có giá 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer,
        [_fact_row(1), _fact_row(1, value=3_100_000_000)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert answer not in tokens
    assert result["answer"] == PRICE_LOOKUP_UNCERTAIN
    assert result["confidence"] == "LOW"


# --- limitation turns publish no evidence at all ----------------------------


def test_limitation_turn_publishes_no_evidence_frames(monkeypatch):
    result, _, events = _run(
        monkeypatch, "Giá căn CH-03?", "Căn CH-03 có giá 2.850.000.000 đồng.", [],
    )
    assert result["answer"] == NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-03")
    assert _frames(events).isdisjoint({SSE_EVENT_FACTS, SSE_EVENT_IMAGES})
    assert result["facts"] == []
    assert result["images"] == []


def test_non_price_turn_with_an_orphan_figure_uses_the_grounding_copy(monkeypatch):
    result, tokens, _ = _run(
        monkeypatch, "Mô tả chung dự án", "Dự án có 2.850.000.000 đồng quy mô đầu tư.",
        [_fact_row(1)],
    )
    assert result["answer"] == LOW_GROUNDING_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    assert "2.850.000.000" not in tokens



def test_price_question_whose_draft_ignores_it_gets_the_limitation_copy(monkeypatch):
    """A price question answered with an amenities blurb is not an answer.

    The draft verifies fine and carries no orphan figure, so nothing else would
    block it — but publishing it silently ignores what the user asked. Plan
    P0-03C: a generic price question with no confirmed eligible fact gets the
    generic limitation copy instead.
    """
    blurb = "Dự án có hồ bơi, phòng gym và vườn cây xanh."
    result, tokens, _ = _run(
        monkeypatch, "Giá bán chung dự án?", blurb, [],
        probe=ConfirmedPriceProbe(True, None, frozenset(), False),
    )
    assert blurb not in tokens
    assert result["answer"] == NO_CONFIRMED_PROJECT_PRICE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


# --- loan/debt is never licensed by a same-valued price ---------------------
#
# db/schema.sql derives loan_amount_vnd from price and deposit, so a loan figure
# is NUMERICALLY equal to the price row far more often than by accident. Price
# authority must not extend to it: a clause that names a loan, not a price,
# gets no field scope at all.


@pytest.mark.parametrize(
    "answer",
    [
        "Căn CH-03 có khoản vay 2.850.000.000 đồng.",
        "Số tiền vay 2.850.000.000 đồng.",
        "Dư nợ 2.850.000.000 đồng.",
        "Gốc vay 2.850.000.000 đồng.",
    ],
)
def test_loan_figure_equal_to_the_price_does_not_publish(monkeypatch, answer):
    result, tokens, _ = _run(
        monkeypatch, "Thông tin tài chính căn CH-03?", answer, [_fact_row(1)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert "2.850.000.000" not in tokens
    assert "2.850.000.000" not in result["answer"]
    assert result["answer"] != answer
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_a_named_price_still_publishes_against_the_same_row(monkeypatch):
    """The opposite control: the allowlist must not over-block real prices."""
    answer = "Căn CH-03 có giá 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [_fact_row(1)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert tokens == answer
    assert result["answer"] == answer


def test_a_cue_less_money_figure_binds_to_nothing(monkeypatch):
    """No recognised cue at all is not a price, in any wording."""
    answer = "Chi phí ban đầu của căn CH-03 là 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Chi phí căn CH-03?", answer, [_fact_row(1)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert "2.850.000.000" not in tokens
    assert result["answer"] != answer
    assert result["confidence"] == "LOW"

def test_gia_dinh_does_not_license_a_price(monkeypatch):
    """F regression: accent-folded 'gia dinh' must not match the bare 'gia' price cue."""
    answer = "Vốn gia đình 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Vốn gia đình căn CH-03?", answer, [_fact_row(1)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert "2.850.000.000" not in tokens
    assert result["answer"] != answer
    assert result["confidence"] == "LOW"


@pytest.mark.parametrize(
    "answer",
    [
        "Căn CH-03 có giá 2.850.000.000 đồng, tức 2,85 tỷ.",
        "Giá bán căn hộ 2.850.000.000 đồng.",
        "Bảng giá 2.850.000.000 đồng.",
    ],
)
def test_price_cues_still_publish(monkeypatch, answer):
    """F opposite control: real price cues keep licensing price_vnd."""
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [_fact_row(1)],
        probe=ConfirmedPriceProbe(True, None, frozenset({"CH-03"}), True),
    )
    assert tokens == answer
    assert result["answer"] == answer
