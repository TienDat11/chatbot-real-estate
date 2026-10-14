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
from decimal import Decimal

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


# --- H: a labelled range answers a price question with no confirmed exact price


def _range_row(fact_id: int = 1, *, policy_key: str = "htls") -> dict:
    return _fact_row(
        fact_id, value=None, quality="range", trust="estimate", policy_key=policy_key,
        range_min=1_980_000_000, range_max=2_640_000_000,
    )


def test_labelled_range_answers_a_price_question_with_no_confirmed_price(monkeypatch):
    """The indicative board is the answer: no "chưa có giá" copy over real evidence."""
    answer = (
        "Căn CH-03 có giá ước tính từ 1.980.000.000 đồng đến 2.640.000.000 đồng "
        "theo phương án HTLS."
    )
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [_range_row()],
        probe=ConfirmedPriceProbe(True, None, frozenset(), False),
    )
    assert tokens == answer
    assert result["answer"] == answer
    assert "1.980.000.000" in tokens
    assert "2.640.000.000" in tokens
    assert "HTLS" in tokens
    assert result["answer"] != NO_CONFIRMED_PROJECT_PRICE
    assert result["confidence"] in {"MEDIUM", "LOW"}
    assert result["requires_review"] is True


def test_range_row_never_licenses_an_exact_single_price(monkeypatch):
    """A range row authorizes its bounds under a label, never one figure as the price."""
    answer = "Căn CH-03 có giá 2.850.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [_range_row()],
        probe=ConfirmedPriceProbe(True, None, frozenset(), False),
    )
    assert answer not in tokens
    assert result["answer"] != answer
    assert result["answer"] != NO_CONFIRMED_PROJECT_PRICE
    assert result["confidence"] == "LOW"


def test_range_bounds_are_not_licensed_by_a_mismatched_range(monkeypatch):
    """Stated bounds must equal the row's bounds; a wider range is refused."""
    answer = "Căn CH-03 có giá ước tính từ 1.500.000.000 đồng đến 2.640.000.000 đồng."
    result, tokens, _ = _run(
        monkeypatch, "Giá căn CH-03?", answer, [_range_row()],
        probe=ConfirmedPriceProbe(True, None, frozenset(), False),
    )
    assert answer not in tokens
    assert result["answer"] != answer


def test_range_claim_binds_when_a_non_price_key_comes_first():
    """Binder regression: the scoped key is not necessarily the first key.

    ``build_estimate_evidence`` puts ``display_name`` first, and the range path
    once read only the first key, so no range claim could ever bind. Any key in
    ``fields`` that the clause licenses must authorize the bounds.
    """
    # display_name first: the pre-fix code read only the first key and refused.
    row = {
        "unit": "vnd",
        "quality": "range",
        "trust_level": "estimate",
        "range": {"min": 1_980_000_000, "max": 2_640_000_000},
        "fields": {
            "display_name": "Căn hộ Studio",
            "price_min_vnd": 1_980_000_000,
            "price_max_vnd": 2_640_000_000,
        },
    }
    clause = "Căn hộ Studio có giá ước tính từ 1.980.000.000 đồng đến 2.640.000.000 đồng"
    spans = [
        (Decimal("1980000000"), None, 0, 0),
        (Decimal("2640000000"), None, 0, 0),
    ]
    assert workflow_module._bind_claim(row, clause, spans) is True


# --- G: the price answer survives the model's own closing prose -------------
#
# A price answer presents one band per payment method, then the model adds a
# short benefit summary. The summary is written by COMBINING bounds from several
# methods ("từ 1,72 tỷ đến 2,64 tỷ"), and no single fact row states that span, so
# the claim is refused. Refusing the claim used to delete the whole answer,
# including the per-method bands that bound perfectly -- the customer asked for a
# price and got "cannot confirm".


def _band_row(
    fact_id: int,
    *,
    unit_code: str,
    policy_key: str,
    low: int,
    high: int,
) -> dict:
    """A labelled indicative band as the pipeline actually projects it.

    The binder reads the PROJECTED row (``build_fact_evidence`` output), not the
    raw SQL row, so a hand-built dict here would test a shape no turn ever sees.
    """
    return build_fact_evidence(
        [_fact_row(
            fact_id,
            unit_code=unit_code,
            policy_key=policy_key,
            fact_key="price_vnd",
            value=None,
            quality="range",
            trust="estimate",
            range_min=low,
            range_max=high,
        )],
        "facts",
        TODAY,
    )[0]


def test_ungrounded_closing_prose_does_not_delete_the_price_bands():
    """The repair keeps every bound band and drops only the invented span.

    The dropped sentence is the one carrying the unbound figure; the bands
    survive byte for byte. The remainder is re-bound before it is offered, so
    this can never launder a figure.
    """
    answer = (
        "1. **Căn hộ Studio**\n"
        "   - **Phương án thanh toán sớm 95%:** Khoảng 1,72 - 2,30 tỷ đồng.\n"
        "\n"
        "*So sánh nhanh:* Căn Studio là lựa chọn tối ưu dòng vốn nhất "
        "(chỉ từ 1,72 tỷ đồng), căn 1.5PN đáp ứng tốt nhu cầu ở linh hoạt "
        "(từ 2,85 tỷ đồng)."
    )
    rows = [
        _band_row(1, unit_code="studio", policy_key="som95", low=1_720_000_000, high=2_300_000_000),
        _band_row(2, unit_code="1p1", policy_key="som95", low=2_850_000_000, high=3_580_000_000),
    ]
    repaired = workflow_module._drop_ungrounded_sentences(
        answer, rows, "Giá căn hộ The Camellia Sơn Trà bao nhiêu?", "camellia", None
    )
    assert repaired is not None
    # The band that grounds survives with its exact text.
    assert "Khoảng 1,72 - 2,30 tỷ đồng" in repaired
    # The invented span is gone with the sentence that stated it.
    assert "1,72 tỷ đồng), căn 1.5PN" not in repaired
    # And the remainder is fully bound -- the repair is never a laundering step.
    bound, total, _ = workflow_module._bind_financial_claims(
        rows, repaired, "Giá căn hộ The Camellia Sơn Trà bao nhiêu?", "camellia", None
    )
    assert total > 0
    assert total == len(bound)


def test_repair_refuses_when_nothing_would_remain_grounded():
    """A draft whose ONLY sentence is ungrounded still gets the canned copy."""
    answer = "*So sánh nhanh:* Căn hộ dao động từ 1,72 tỷ đến 2,64 tỷ đồng."
    rows = [
        _band_row(1, unit_code="studio", policy_key="som95", low=1_720_000_000, high=2_300_000_000),
    ]
    assert (
        workflow_module._drop_ungrounded_sentences(
            answer, rows, "Giá căn hộ?", "camellia", None
        )
        is None
    )


def test_a_fully_grounded_answer_is_never_repaired():
    """No ungrounded span means no rewrite at all: the string is untouched."""
    answer = (
        "1. **Căn hộ Studio**\n"
        "   - **Phương án thanh toán sớm 95%:** Khoảng 1,72 - 2,30 tỷ đồng.\n"
    )
    rows = [
        _band_row(1, unit_code="studio", policy_key="som95", low=1_720_000_000, high=2_300_000_000),
    ]
    assert (
        workflow_module._drop_ungrounded_sentences(
            answer, rows, "Giá căn hộ?", "camellia", None
        )
        is None
    )


# --- H: the two gates agree about a policy label's own percentage -----------
#
# "Phương án thanh toán sớm 95%" is a method's NAME, and the figure belongs to
# the label rather than to the answer's arithmetic. The claim binder already
# excluded it; the orphan recomputation did not, so every method-labelled answer
# carried an orphan the binder could never bind and the publish gate refused a
# correct answer as ungrounded.


def test_policy_label_percentage_is_not_an_orphan_figure():
    """The method's NAME percentage must not be reported as an ungrounded figure.

    The band's own bounds are still orphans to the verifier -- a range row carries
    no value in ``fields``, which is documented and handled by the bound-span
    accounting. What must NOT appear is the "95%" inside the policy label: the
    binder excludes it, so counting it here made the two gates disagree and every
    method-labelled answer was refused as ungrounded.
    """
    answer = (
        "1. **Căn hộ Studio**\n"
        "   - **Phương án thanh toán sớm 95%:** Khoảng 1,72 - 2,30 tỷ đồng.\n"
    )
    facts = [_band_row(1, unit_code="studio", policy_key="som95", low=1_720_000_000, high=2_300_000_000)]
    orphans = workflow_module._verifier_orphan_spans(answer, facts, None)
    orphan_texts = [answer[start:end] for start, end in orphans]
    assert "95%" not in orphan_texts
    assert not any("%" in text for text in orphan_texts)


# --- I: one clause may state two different percentage commitments -----------
#
# The HTLS plan writes the deposit and the rate in one clause ("vốn tự có 30%,
# lãi suất 0% trong 18 tháng"). Grouping both figures into one claim left the
# figure whose cue the clause did not name first unbound, and one unbound figure
# refuses the whole clause -- so the rate could never be quoted beside the
# deposit it belongs with.


def test_two_percentage_commitments_in_one_clause_split_by_their_own_cue():
    """Each figure is classified by its own comma segment, not the whole clause.

    The money band, the deposit percentage and the rate become THREE claims: no
    single row licenses a deposit and a rate together, so a merged claim would be
    refused as a whole and the rate could never be stated beside the deposit it
    belongs with.
    """
    clause = "khoảng 1,98 - 2,64 tỷ đồng (vốn tự có 30%, lãi suất 0% trong 18 tháng)"
    claims = workflow_module._financial_claims(clause)
    percent_claims = [
        sorted((str(span[0]), span[1]) for span in spans)
        for _clause, spans in claims
        if all(span[1] == "%" for span in spans)
    ]
    assert sorted(percent_claims) == [[("0", "%")], [("30", "%")]]
    # The band is its own claim, and it is not merged with either percentage.
    assert any(
        all(span[1] != "%" for span in spans) and len(spans) == 2 for _clause, spans in claims
    )


def test_deposit_percentage_beside_a_rate_is_classified_by_its_own_segment():
    """A figure's cue comes from its own comma segment, not the clause it shares.

    ``_claim_field_scope`` reads one cue per clause, and one clause states several
    commitments ("đặt cọc 30%, lãi suất 0% trong 18 tháng"). Read from the whole
    clause the rate cue wins, so the deposit figure found no licensed field and
    the whole clause was refused. The segment is what a figure's own commitment
    is written in.
    """
    clause = "đặt cọc 30%, lãi suất 0% trong 18 tháng"
    # Whole clause: the rate cue governs, so the deposit finds no field at all.
    assert workflow_module._claim_field_scope(clause, True) == frozenset({"interest_rate_pct"})
    deposit_start = clause.index("30%")
    rate_start = clause.index("0% trong")
    assert workflow_module._pct_cue_class(
        workflow_module._pct_segment_clause(clause, deposit_start, deposit_start + 3)
    ) == "deposit"
    assert workflow_module._pct_cue_class(
        workflow_module._pct_segment_clause(clause, rate_start, rate_start + 2)
    ) == "rate"


def test_an_unrecognised_commitment_word_still_refuses_its_percentage():
    """A cue the registry does not know binds to nothing — the allowlist holds.

    "vốn tự có" is the buyer's own capital, but no fact field is registered under
    that name, so the figure stays unbound rather than being guessed onto the
    deposit row it resembles. Refusing is the correct outcome, not a gap.
    """
    clause = "khoảng 1,98 - 2,64 tỷ đồng (vốn tự có 30%, lãi suất 0% trong 18 tháng)"
    deposit_start = clause.index("30%")
    segment = workflow_module._pct_segment_clause(clause, deposit_start, deposit_start + 3)
    assert workflow_module._pct_cue_class(segment) == "other"
    assert workflow_module._claim_field_scope(segment, True) == frozenset()


# --- J: a policy-wide percentage binds without naming a unit -----------------
#
# An answer opens with the project's policy ("dự án The Camellia đang áp dụng
# chính sách hỗ trợ lãi suất với mức 0%/năm trong 18 tháng"), which names no
# unit. The identity walk then had no subject to follow and refused the figure,
# so a correct project-level rate was canned. A rate IS a policy-wide term, so
# every candidate row of that policy must carry it; a price is per-unit evidence
# and stays subject-bound.


def _pct_row(
    fact_id: int,
    *,
    unit_code: str = "studio",
    policy_key: str = "htls",
    fact_key: str = "interest_rate_pct",
    value: int = 0,
    unit: str = "pct",
) -> dict:
    """A percentage fact as the pipeline projects it, not as the DB stores it."""
    return build_fact_evidence(
        [_fact_row(
            fact_id, unit_code=unit_code, policy_key=policy_key,
            fact_key=fact_key, value=value, unit=unit,
        )],
        "facts",
        TODAY,
    )[0]


def test_policy_level_rate_binds_when_every_unit_of_that_policy_agrees():
    """The clause names the policy and no unit; every unit carries the same rate."""
    rows = [
        _pct_row(1, unit_code="studio"),
        _pct_row(2, unit_code="1p1"),
        _pct_row(3, unit_code="2pn-noi-khu"),
    ]
    clause = "dự án The Camellia áp dụng chính sách hỗ trợ lãi suất với mức 0%/năm"
    spans = workflow_module._financial_claims(clause)[0][1]
    assert workflow_module._bind_claim_across_policy(rows, clause, spans, "htls") is True


def test_policy_level_rate_is_refused_when_one_unit_disagrees():
    """One unit carrying a different rate makes the claim a generalization."""
    rows = [
        _pct_row(1, unit_code="studio", value=0),
        _pct_row(2, unit_code="1p1", value=3),
    ]
    clause = "dự án The Camellia áp dụng chính sách hỗ trợ lãi suất với mức 0%/năm"
    spans = workflow_module._financial_claims(clause)[0][1]
    assert workflow_module._bind_claim_across_policy(rows, clause, spans, "htls") is False


def test_policy_level_money_claim_is_never_bound_across_units():
    """A price is per-unit evidence: a project-level clause must not license it.

    Licensing CH-03's price from a clause that names no unit is exactly how a
    figure would be attributed to CH-11.
    """
    rows = [_fact_row(1, unit_code="CH-03")]
    clause = "dự án The Camellia có mức giá 2.850.000.000 đồng"
    spans = workflow_module._financial_claims(clause)[0][1]
    assert workflow_module._bind_claim_across_policy(rows, clause, spans, "htls") is False


def test_policy_level_rate_ignores_rows_that_do_not_carry_the_field():
    """A ``term_months`` row beside the rate rows cannot veto a rate claim."""
    rows = [
        _pct_row(1, unit_code="studio", value=0),
        _pct_row(2, unit_code="1p1", value=0),
        _pct_row(3, unit_code="studio", fact_key="term_months", value=18, unit="months"),
    ]
    clause = "dự án The Camellia áp dụng chính sách hỗ trợ lãi suất với mức 0%/năm"
    spans = workflow_module._financial_claims(clause)[0][1]
    assert workflow_module._bind_claim_across_policy(rows, clause, spans, "htls") is True


# --- K: a table whose only data row is dropped leaves no empty grid ----------
#
# The repair removes the LINES carrying an unbound figure, and a table's data
# row is one such line. When it was the table's only row the header and
# separator survived alone, shipping a malformed empty table to the reader.


def test_dropped_table_row_takes_its_header_with_it():
    text = "Mở đầu.\n\n| Loại căn hộ | Lãi suất |\n| :--- | :---: |\n\nKết."
    stripped = workflow_module._strip_dangling_table_headers(text)
    assert "|" not in stripped
    assert "Mở đầu." in stripped and "Kết." in stripped


def test_a_table_with_a_data_row_keeps_its_header():
    text = "| A | B |\n| --- | --- |\n| 1 | 2 |\nSau."
    assert workflow_module._strip_dangling_table_headers(text) == text


def test_text_without_a_table_is_untouched():
    text = "Đoạn văn không bảng."
    assert workflow_module._strip_dangling_table_headers(text) == text


def test_repair_drops_the_orphan_row_and_the_empty_table_with_it():
    """End to end: the unbound row goes, and no header survives it."""
    answer = (
        "Giá tham khảo:\n"
        "| Loại căn | Giá |\n"
        "| :--- | :--- |\n"
        "| Studio | 9.999.999.999 đồng |\n"
    )
    rows = [
        _band_row(1, unit_code="studio", policy_key="som95",
                  low=1_720_000_000, high=2_300_000_000),
    ]
    repaired = workflow_module._drop_ungrounded_sentences(
        answer, rows, "Giá căn hộ The Camellia Sơn Trà bao nhiêu?", "camellia", None
    )
    # No groundable figure survives, so the repair declines and the canned copy
    # stays; the point is that it never returns a bare header.
    assert repaired is None or "|" not in repaired
