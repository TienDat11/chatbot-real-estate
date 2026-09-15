"""GA-04: deterministic global project resolution (contract tests).

Black-box against the resolution precedence ladder (explicit_query >
user_selection > route_context > session_context > unresolved) and the safety
invariants: ambiguous never selects, unknown hints never become scope, and no
default project is ever used for an unresolved customer query.

Tests inject the active catalogue (key, ten_thuong_mai) directly so they are
hermetic — no Postgres, no seed fallback, no DEFAULT_PROJECT_KEY leak.
"""

from __future__ import annotations

import pytest

from api.application.services.global_project_resolution import (
    ProjectResolution,
    ProjectResolutionSource,
    ProjectResolutionStatus,
    resolve_global_project,
)

# Active catalogue mirroring db/seed/project_config.sql order (camellia first).
# ten_truong_mai is the picker label; camellia uses the ASCII seed spelling.
CATALOGUE: list[tuple[str, str]] = [
    ("camellia", "The Camellia Son Tra - Da Nang"),
    ("soleil", "The Soleil Đà Nẵng"),
]


@pytest.mark.asyncio
async def test_explicit_camellia_query_resolves_camellia() -> None:
    """A query naming Camellia resolves to camellia via explicit_query.

    Also pins the P0 fix: the degenerate-prefix query
    "Thế số điện thoại của Camellia là gì?" contains the truncated "the came"
    needle plus the real "camellia" key needle — it must resolve to camellia
    (not ambiguous), proving _safe_needles keeps genuine matches while dropping
    the truncated garbage.
    """
    res = await resolve_global_project(
        query="Bạn biết gì về Camellia không?",
        selected_project_key=None,
        route_project_key=None,
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.RESOLVED
    assert res.project_key == "camellia"
    assert res.source == ProjectResolutionSource.EXPLICIT_QUERY
    assert res.candidates == ("camellia",)

    res_degenerate = await resolve_global_project(
        query="Thế số điện thoại của Camellia là gì?",
        selected_project_key=None,
        route_project_key=None,
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res_degenerate.status == ProjectResolutionStatus.RESOLVED
    assert res_degenerate.project_key == "camellia"
    assert res_degenerate.source == ProjectResolutionSource.EXPLICIT_QUERY
    assert res_degenerate.candidates == ("camellia",)

    res_lower = await resolve_global_project(
        query="dự án camellia ở đâu",
        selected_project_key=None,
        route_project_key=None,
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res_lower.status == ProjectResolutionStatus.RESOLVED
    assert res_lower.project_key == "camellia"


@pytest.mark.asyncio
async def test_explicit_soleil_query_resolves_soleil() -> None:
    """A query naming Soleil resolves to soleil via explicit_query.

    Also pins the P0 fix does not over-prune: the full marketing label with
    parenthetical ("Thông tin The Soleil Đà Nẅang") is a legitimate needle that
    the mid-word filter must KEEP (its prefix ends on a word boundary), so the
    turn still resolves to soleil.
    """
    res = await resolve_global_project(
        query="giá The Soleil bao nhiêu?",
        selected_project_key="camellia",  # would otherwise win selection — query wins
        route_project_key=None,
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.RESOLVED
    assert res.project_key == "soleil"
    assert res.source == ProjectResolutionSource.EXPLICIT_QUERY
    assert res.candidates == ("soleil",)

    res_full = await resolve_global_project(
        query="Thông tin The Soleil Đà Nẵng",
        selected_project_key=None,
        route_project_key=None,
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res_full.status == ProjectResolutionStatus.RESOLVED
    assert res_full.project_key == "soleil"
    assert res_full.candidates == ("soleil",)


@pytest.mark.asyncio
async def test_explicit_query_wins_over_route_hint() -> None:
    """Explicit query always beats a lower precedence route hint."""
    res = await resolve_global_project(
        query="Camellia giá bao nhiêu?",
        selected_project_key=None,
        route_project_key="soleil",
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.RESOLVED
    assert res.project_key == "camellia"
    assert res.source == ProjectResolutionSource.EXPLICIT_QUERY
    assert res.candidates == ("camellia",)


@pytest.mark.asyncio
async def test_user_selection_used_when_query_has_no_project() -> None:
    """When the query doesn't name a project, the user-selected key wins."""
    res = await resolve_global_project(
        query="giá bao nhiêu?",
        selected_project_key="soleil",
        route_project_key="camellia",
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.RESOLVED
    assert res.project_key == "soleil"
    assert res.source == ProjectResolutionSource.USER_SELECTION
    assert res.candidates == ("soleil",)


@pytest.mark.asyncio
async def test_route_hint_used_when_query_and_selection_missing() -> None:
    """Route hint resolves when query has no project and no selection."""
    res = await resolve_global_project(
        query="bảng giá căn 2PN",
        selected_project_key=None,
        route_project_key="camellia",
        session_project_key="soleil",  # lower precedence than route
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.RESOLVED
    assert res.project_key == "camellia"
    assert res.source == ProjectResolutionSource.ROUTE_CONTEXT
    assert res.candidates == ("camellia",)


@pytest.mark.asyncio
async def test_session_context_used_last() -> None:
    """Session active project resolves only when higher levels are empty."""
    res = await resolve_global_project(
        query="khi nào bàn giao?",
        selected_project_key=None,
        route_project_key=None,
        session_project_key="soleil",
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.RESOLVED
    assert res.project_key == "soleil"
    assert res.source == ProjectResolutionSource.SESSION_CONTEXT
    assert res.candidates == ("soleil",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        # No signal at all -> unresolved.
        "hỗ trợ thêm không?",
        # --- P0 regression: degenerate truncated needle must not bind scope ---
        # `project_match_variants` emits truncated prefix garbage ("the so" for
        # Soleil, "the came" for Camellia) via a stem-relative offset bug; these
        # project-less phrasings normalize to contain that garbage but never name
        # a real project key, so they must stay unresolved (never a default).
        "Thế số điện thoại của dự án là gì?",
        "Thế số lượng căn hộ còn lại là bao nhiêu?",
        "The source of this project data?",
        "the camera is broken?",
    ],
)
async def test_no_context_is_unresolved(query: str) -> None:
    """No signal at all -> unresolved, never a default project.

    Also pins the P0 regression: queries whose normalized text contains a
    truncated prefix needle (e.g. "the so", "the came") emitted by the upstream
    variant builder must NOT bind scope to a project the customer never named.
    """
    res = await resolve_global_project(
        query=query,
        selected_project_key=None,
        route_project_key=None,
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.UNRESOLVED
    assert res.project_key is None
    assert res.source == ProjectResolutionSource.NONE
    assert res.candidates == ()


@pytest.mark.asyncio
async def test_multiple_explicit_projects_is_ambiguous() -> None:
    """A query naming two known projects -> ambiguous, no project selected."""
    res = await resolve_global_project(
        query="so sánh camellia với soleil",
        selected_project_key=None,
        route_project_key=None,
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.AMBIGUOUS
    assert res.project_key is None
    assert res.source == ProjectResolutionSource.EXPLICIT_QUERY
    # Both deterministic matches are reported as candidates; order follows the
    # catalogue (camellia first), never "first foreign project" promotion.
    assert res.candidates == ("camellia", "soleil")


@pytest.mark.asyncio
async def test_unknown_route_hint_does_not_resolve() -> None:
    """An unknown/inactive route hint is ignored, not promoted to resolved."""
    res = await resolve_global_project(
        query="giá bao nhiêu?",
        selected_project_key=None,
        route_project_key="vinhomes",
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.UNRESOLVED
    assert res.project_key is None
    assert res.source == ProjectResolutionSource.NONE
    assert res.candidates == ()


@pytest.mark.asyncio
async def test_unknown_selected_project_does_not_resolve() -> None:
    """An unknown/inactive selected key is ignored, not promoted to resolved."""
    res = await resolve_global_project(
        query="hôm nay có khuyến mãi không?",
        selected_project_key="vinhomes",
        route_project_key=None,
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.UNRESOLVED
    assert res.project_key is None
    assert res.source == ProjectResolutionSource.NONE
    assert res.candidates == ()


@pytest.mark.asyncio
async def test_no_default_project_is_used() -> None:
    """The resolver NEVER falls back to a default project.

    Even with the real active catalogue present, an unresolved customer query
    (no mention, no selection, no route/session hint) must stay unresolved —
    never camellia-as-default. An empty catalogue is also unresolved.
    """
    # Real catalogue present but no signals: must NOT default to camellia.
    res = await resolve_global_project(
        query="với cảm xúc thời tiết hôm nay thế nào",
        selected_project_key=None,
        route_project_key=None,
        session_project_key=None,
        known_projects=CATALOGUE,
    )
    assert res.status == ProjectResolutionStatus.UNRESOLVED
    assert res.project_key is None
    assert res.source == ProjectResolutionSource.NONE
    assert res.candidates == ()

    # Empty active catalogue (dead DB): unresolved, not a seeded default.
    res_empty = await resolve_global_project(
        query="với cảm xúc thời tiết hôm nay thế nào",
        selected_project_key=None,
        route_project_key=None,
        session_project_key=None,
        known_projects=[],
    )
    assert res_empty.status == ProjectResolutionStatus.UNRESOLVED
    assert res_empty.project_key is None
    assert res_empty.source == ProjectResolutionSource.NONE
    assert res_empty.candidates == ()


@pytest.mark.asyncio
async def test_leading_article_only_needle_does_not_bind_project() -> None:
    """A needle consisting only of a leading article (``the``) must not bind scope.

    Regression for GA-04: ``project_match_variants`` emits a bare article as a
    needle when the project's brand token is exactly 3 chars (e.g. ``sun`` /
    "The Sun City" yields needles ``('sun', 'the sun city', 'the')``). The bare
    ``the`` survived ``_safe_needles`` and matched any query containing it,
    silently resolving an unresolved turn to a project the customer never named.

    With a synthetic 3-char-token catalogue entry, a query that never names the
    project must stay unresolved (no default, no guessed scope).
    """
    catalogue: list[tuple[str, str]] = [("sun", "The Sun City")]
    res = await resolve_global_project(
        query="the camera is broken?",
        selected_project_key=None,
        route_project_key=None,
        session_project_key=None,
        known_projects=catalogue,
    )
    assert res.status == ProjectResolutionStatus.UNRESOLVED
    assert res.project_key is None
    assert res.source == ProjectResolutionSource.NONE
    assert res.candidates == ()
