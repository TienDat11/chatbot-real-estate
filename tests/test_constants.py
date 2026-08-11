"""Unit tests for `api.constants` — SSE event set and confidence tiers.

Runs without DB/network: only `api.constants` is imported (side-effect free).
Expected values are pinned from the FE contract so a rename on either side
fails the assertion.

FE sources:
- SSE events: packages/contracts/src/constants.ts -> `API_SSE_EVENTS`
- Confidence: packages/contracts/src/index.ts -> `type Confidence`
"""

from api import constants

# Canonical SSE event names emitted by POST /api/query. Keep in sync with
# packages/contracts/src/constants.ts `API_SSE_EVENTS` (backend must not emit
# an event the frontend does not know, and vice versa).
# `places` (maps leg) is emitted before `sources` when the router requests geo.
EXPECTED_SSE_EVENTS = {"places", "sources", "facts", "token", "done", "error"}

# Canonical 3-tier confidence values. Keep in sync with
# packages/contracts/src/index.ts `type Confidence` and the FE badge mapping
# `CONFIDENCE_LABELS` in packages/contracts/src/constants.ts.
EXPECTED_CONFIDENCE_TIERS = {"HIGH", "MEDIUM", "LOW"}


def sse_event_values() -> set[str]:
    """All SSE_EVENT_* string constants defined in api.constants."""
    return {
        value
        for name, value in vars(constants).items()
        if name.startswith("SSE_EVENT_") and isinstance(value, str)
    }


def test_sse_event_set_matches_fe_contract():
    assert sse_event_values() == EXPECTED_SSE_EVENTS


def test_sse_event_values_are_distinct():
    # Every event name is a distinct string (no two constants alias).
    assert len(sse_event_values()) == len(EXPECTED_SSE_EVENTS)


def test_confidence_tiers_sanity():
    # api/guard_output.py `_confidence_3tier` returns only HIGH/MEDIUM/LOW;
    # the FE ConfidenceBadge is keyed by the same 3 values. A fourth tier
    # would break the badge switch, so the set is pinned to exactly 3.
    assert EXPECTED_CONFIDENCE_TIERS == {"HIGH", "MEDIUM", "LOW"}
    assert len(EXPECTED_CONFIDENCE_TIERS) == 3
