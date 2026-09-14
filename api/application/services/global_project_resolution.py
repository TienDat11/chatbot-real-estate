"""Deterministic global project resolution (GA-04).

Resolves the project context to use for a Global Assistant turn BEFORE any
project-specific retrieval runs. Resolution is a pure precedence ladder over
explicit signals — no LLM, no embeddings, no fuzzy typo guessing, and
critically NO ``DEFAULT_PROJECT_KEY`` fallback: when no signal resolves a
project the status is ``unresolved`` and the caller must ask the customer to
choose, never silently answering from any project's corpus (`production_safety.
forbidden_release_behaviour`: "using a default project for unresolved customer
query").

Precedence (explicit_query > user_selection > route_context > session_context):

1. explicit project mention in the current query  -> ``explicit_query``
2. explicit user-selected project                  -> ``user_selection``
3. route project context                           -> ``route_context``
4. session active project                          -> ``session_context``
5. nothing resolved                                 -> ``unresolved`` / ``none``

Matching delegates to the existing deterministic normalisation/variant logic
in :mod:`api.domain.services.project_redirect` (``normalize_project_text`` and
``project_match_variants``) so the keyword vocabulary stays single-sourced —
the cross-project guard and this resolver can never drift about what names a
project answers to.

The active catalogue is the single source of truth for which project keys are
valid. It is injectable for hermetic tests; when omitted the resolver reads
``fetch_active_projects()`` directly (best-effort: a dead DB yields no
candidates, i.e. ``unresolved`` — the resolver deliberately does NOT fall back
to the static seed mirror or ``DEFAULT_PROJECT_KEY``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from api.domain.services.project_redirect import (
    normalize_project_text,
    project_match_variants,
)

__all__ = [
    "ProjectResolutionStatus",
    "ProjectResolutionSource",
    "ProjectResolution",
    "candidates_for",
    "resolve_global_project",
]


class ProjectResolutionStatus(StrEnum):
    """Outcome of resolving a project for one turn."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


class ProjectResolutionSource(StrEnum):
    """Origin of a resolved/unresolved project (the contract source field)."""

    EXPLICIT_QUERY = "explicit_query"
    USER_SELECTION = "user_selection"
    ROUTE_CONTEXT = "route_context"
    SESSION_CONTEXT = "session_context"
    NONE = "none"


@dataclass(frozen=True)
class ProjectResolution:
    """Deterministic resolution result consumed by the query orchestration.

    ``project_key`` is the resolved key only when ``status == RESOLVED``; it is
    ``None`` for ``unresolved`` and ``ambiguous``. ``candidates`` carries every
    matched key (all deterministic matches for ambiguous, or the chosen key
    wrapped in a 1-tuple for resolved) so callers can render a disambiguating
    picker or an "ask again" prompt without re-deriving matches.
    """

    status: ProjectResolutionStatus
    project_key: str | None
    source: ProjectResolutionSource
    candidates: tuple[str, ...]


def _is_word_boundary(ch: str) -> bool:
    """True when ``ch`` ends an alphanumeric run — i.e. not mid-word.

    Uses the same ASCII ``[a-z0-9]`` word-run semantics as the upstream matcher
    (``_WORD_RUN = re.compile(r"[a-z0-9]+")``): after ``normalize_project_text``
    all needles are ASCII lowercase + spaces/dashes/parens, so an ASCII
    predicate matches the matcher's notion of "same word" exactly. A Unicode
    ``isalnum()`` would be broader and could wrongly treat a non-ASCII char as a
    word char, so we avoid it here.
    """
    if ch == "" or ch in (" ", "-", "("):
        return True
    return not (ch.isascii() and (ch.isalpha() or ch.isdigit()))


def _safe_needles(needles: tuple[str, ...]) -> tuple[str, ...]:
    """Drop degenerate truncated needles before they bind scope.

    ``project_match_variants`` builds a prefix needle + bare token per project,
    but slices the prefix from the WHOLE name using an offset computed on the
    brand stem (``stem = normalized_name[4:] if startswith "the " else ...``).
    That offset lives in a different coordinate space than ``normalized_name``,
    so the prefix slice lands mid-word — e.g. for "The Soleil Đà Nẵng (…)" it
    emits the 6-char garbage needle ``the so``, and for "The Camellia Son Tra -
    Da Nang" it emits ``the came``.

    Those truncated needles are false substring matches against arbitrary
    queries ("Thế số điện thoại của dự án là gì?" normalizes to contain
    "the so") and would silently bind an unresolved turn to a project the
    customer never named — the exact forbidden behaviour the resolver exists to
    prevent. We cannot fix the offset bug here (project_redirect.py behaviour
    changes are forbidden by this ticket), so we filter degenerate needles at
    the consumer boundary.

    Rule: drop needle ``v`` only when another needle ``w`` (same project,
    ``w != v``) starts with ``v`` AND the character immediately after ``v``
    inside ``w`` is mid-word (not a word boundary). A blanket "drop any strict
    prefix" filter is wrong: ``the soleil da nang`` is a legitimate strict
    prefix of the parenthetical ``the soleil da nang (bo suu tap ...)`` and must
    be kept — its next character is a space, so the mid-word rule preserves it
    while discarding only the truncated garbage (``the so`` / ``the came``).
    """
    keep: list[str] = []
    for v in needles:
        degenerate = False
        for w in needles:
            if w == v or not w.startswith(v):
                continue
            # Character immediately after v inside the longer needle w.
            boundary = w[len(v)] if len(v) < len(w) else ""
            if not _is_word_boundary(boundary):
                degenerate = True
                break
        if not degenerate:
            if v and v not in keep:
                keep.append(v)
    return tuple(keep)


def candidates_for(
    query: str | None,
    known_projects: list[tuple[str, str]],
) -> tuple[str, ...]:
    """Project keys whose variant needles are deterministic substring matches in ``query``.

    Reuses ``project_match_variants`` (per-project normalized keywords: key,
    full commercial name, brand stem, bare token) so the resolver and the
    cross-project guard read from one matching vocabulary. Matching is
    case/diacritic/space-folded via ``normalize_project_text`` — identical to
    the guard — so user phrasing never breaks detection and unknown third-party
    names ("vinhomes") never match (absent from the catalogue).

    Needles are passed through ``_safe_needles`` to neutralize the truncated
    prefix garbage emitted by ``project_match_variants``'s upstream offset
    bug, so only legitimate normalized needles can bind project scope.

    Returns keys in the catalogue's own order (the caller pins order); the
    resolver treats the returned ordering as a stable candidate set, never as a
    "pick first" signal.
    """
    normalized_query = normalize_project_text(query)
    if not normalized_query:
        return ()
    matched: list[str] = []
    for project_key, display_name in known_projects:
        variants = _safe_needles(project_match_variants(project_key, display_name))
        if any(variant in normalized_query for variant in variants):
            matched.append(project_key)
    return tuple(matched)


def _project_key_active(
    project_key: str | None,
    known_projects: list[tuple[str, str]],
) -> bool:
    """True when ``project_key`` names a key in the active catalogue."""
    if not project_key:
        return False
    return any(key == project_key for key, _ in known_projects)


async def resolve_global_project(
    *,
    query: str,
    selected_project_key: str | None,
    route_project_key: str | None,
    session_project_key: str | None,
    known_projects: list[tuple[str, str]] | None = None,
) -> ProjectResolution:
    """Resolve the project for one Global Assistant turn.

    ``known_projects`` is an ordered list of ``(project_key, ten_thuong_mai)``
    pairs — the live active catalogue (e.g. ``camellia`` first). When ``None``
    the resolver loads it from the registry via ``fetch_active_projects()``;
    a failed/empty read yields no candidates rather than the seed mirror or a
    default, so unresolved turns stay unresolved (no silent default project).

    Precedence is strict: the highest level that yields a valid, catalogue-
    present signal wins. Explicit-query matching is deterministic substring
    matching over per-project normalized variant needles only — no LLM and no
    fuzzy guessing. Two or more explicit matches collapse to ``ambiguous``
    (candidates carry every match); unknown/unselected hints are ignored, never
    promoted to resolved.
    """
    if known_projects is None:
        # Lazy import keeps the module importable by pure unit seams without
        # pulling the registry port graph — but production always supplies a
        # live catalogue read here (fetch_active_projects degrades to []).
        from api.application.services.project_scope import (  # noqa: PLC0415
            fetch_active_projects,
        )

        active = await fetch_active_projects()
        known_projects = [(p.project_key, p.ten_thuong_mai) for p in active]

    # 1. explicit project mention in the current query (always wins).
    explicit_matches = candidates_for(query, known_projects)
    if len(explicit_matches) >= 2:
        return ProjectResolution(
            status=ProjectResolutionStatus.AMBIGUOUS,
            project_key=None,
            source=ProjectResolutionSource.EXPLICIT_QUERY,
            candidates=explicit_matches,
        )
    if len(explicit_matches) == 1:
        return ProjectResolution(
            status=ProjectResolutionStatus.RESOLVED,
            project_key=explicit_matches[0],
            source=ProjectResolutionSource.EXPLICIT_QUERY,
            candidates=explicit_matches,
        )

    # 2. explicit user-selected project (must be active/in-catalogue).
    if _project_key_active(selected_project_key, known_projects):
        return ProjectResolution(
            status=ProjectResolutionStatus.RESOLVED,
            project_key=selected_project_key,
            source=ProjectResolutionSource.USER_SELECTION,
            candidates=(selected_project_key,),
        )

    # 3. route project context (must be active/in-catalogue).
    if _project_key_active(route_project_key, known_projects):
        return ProjectResolution(
            status=ProjectResolutionStatus.RESOLVED,
            project_key=route_project_key,
            source=ProjectResolutionSource.ROUTE_CONTEXT,
            candidates=(route_project_key,),
        )

    # 4. session active project (must be active/in-catalogue).
    if _project_key_active(session_project_key, known_projects):
        return ProjectResolution(
            status=ProjectResolutionStatus.RESOLVED,
            project_key=session_project_key,
            source=ProjectResolutionSource.SESSION_CONTEXT,
            candidates=(session_project_key,),
        )

    # 5. unresolved — never a default project, never a guessed project.
    return ProjectResolution(
        status=ProjectResolutionStatus.UNRESOLVED,
        project_key=None,
        source=ProjectResolutionSource.NONE,
        candidates=(),
    )
