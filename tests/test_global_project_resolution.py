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
    """A query naming Camellia resolves to camellia via explicit_query."""
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


@pytest.mark.asyncio
async def test_explicit_soleil_query_resolves_soleil() -> None:
    """A query naming Soleil resolves to soleil via explicit_query."""
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
async def test_no_context_is_unresolved() -> None:
    """No signal at all -> unresolved, never a default project."""
    res = await resolve_global_project(
        query="hỗ trợ thêm không?",
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
    assert set(res.candidates) == {"camellia", "soleil"}
    assert len(res.candidates) == 2


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
