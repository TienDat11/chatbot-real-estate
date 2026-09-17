"""Transport-neutral orchestration for the Global Assistant (GA-BE-06).

This application service stays free of FastAPI/Starlette/SSE/browser imports so
it can be driven by Zalo or any other transport. The web adapter is a separate,
additive route that wires HTTP concerns around this core.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from api.application.services.chat_history_service import ChatHistoryService
from api.application.services.global_project_resolution import (
    ProjectResolution,
    ProjectResolutionSource,
    ProjectResolutionStatus,
    resolve_global_project,
)
from api.application.services.project_scope import fetch_active_projects


async def _load_live_catalogue() -> list[tuple[str, str]]:
    """Global Assistant authoritative catalogue: live registry only.

    Degraded reads (registry unreachable, DB timeout, empty registry) must fail
    closed — never fall back to a static seed, and never let RAG run unscoped.
    An empty catalogue flows into the resolver, which yields an unresolved
    outcome when no known project matches the query.
    """
    try:
        projects = await fetch_active_projects()
    except Exception:  # noqa: BLE001 — safer unresolved than a guessed RAG scope
        return []
    return [(project.project_key, project.ten_thuong_mai) for project in projects]


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]

# Private key under which the source-event id is persisted inside a turn's
# assistant meta. It is never emitted by the public transport adapter, so a
# downstream replay can be matched by the caller's own session identity only.
_SOURCE_EVENT_META_KEY = "_source_event_key"


def _pending_neutral_intent(messages: list) -> str | None:
    """Return the newest unconsumed clarification's user text, if present."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if getattr(message, "role", None) != "assistant":
            continue
        meta = getattr(message, "meta", None)
        envelope = meta.get("ga_outcome") if isinstance(meta, dict) else None
        if not isinstance(envelope, dict) or envelope.get("resolution_status") not in (
            "unresolved", "ambiguous"
        ):
            return None
        if index == 0 or getattr(messages[index - 1], "role", None) != "user":
            return None
        intent = getattr(messages[index - 1], "content", None)
        return intent if isinstance(intent, str) else None
    return None


class GlobalAssistantPersistenceError(RuntimeError):
    """Raised when a core flow cannot durably persist a turn outcome.

    Persistence failure must not be reported as success: the caller (adapter)
    surfaces a server error instead of a misleading 200 with partial state.
    The internal exception text stays server-side; callers should log it and
    return a generic error to the transport.
    """


class GlobalAssistantRedirectMismatchError(RuntimeError):
    """Raised when a pipeline payload's project_redirect contradicts resolution.

    Per the redirect invariant, a resolved project_key must agree with any
    project_redirect emitted by the pipeline payload — otherwise the turn is
    failed closed (no persistence, no RAG answer served) to avoid returning
    data from an unintended scope.
    """

    def __init__(self, resolved: str, redirected: str) -> None:
        self.resolved = resolved
        self.redirected = redirected
        super().__init__(
            f"project_redirect mismatch: resolved={resolved!r} redirected={redirected!r}"
        )


@dataclass(frozen=True)
class GlobalAssistantInput:
    query: str
    session_id: str
    identity_key: str
    device_id: str | None = None
    route_project_key: str | None = None
    selected_project_key: str | None = None
    source_event_key: str | None = None
    on_event: EventCallback | None = None


@dataclass(frozen=True)
class GlobalAssistantOutcome:
    answer: str
    resolution_status: str
    resolution_source: str
    candidates: tuple[str, ...] = ()
    project_key: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    rag_ran: bool = False
    replayed: bool = False
    trace_id: str = ""
    latency_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "resolution_status": self.resolution_status,
            "resolution_source": self.resolution_source,
            "candidates": list(self.candidates),
            "project_key": self.project_key,
            "meta": {k: v for k, v in self.meta.items() if k != _SOURCE_EVENT_META_KEY},
            "rag_ran": self.rag_ran,
            "replayed": self.replayed,
            "trace_id": self.trace_id,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class PreparedTurn:
    """Resolved turn data ready for pipeline execution, carrying the authoritative catalogue.

    ``request`` is the original input so the adapter can retry with a different
    quota context if needed. ``resolution`` is the resolved/unresolved result.
    ``known_projects`` is the fail-closed catalogue that fed resolution and
    that MUST be reused by the pipeline guardrail (no second lookup). ``messages``
    is the already-loaded global message list so replay/execute never re-fetch.
    ``history`` is the bounded, server-side retrieval history for the resolved
    project (already filtered to neutral + resolved-project turns, capped at 8).
    ``replay`` is set when a prior source-event outcome was matched — in that case
    no pipeline or quota is needed.
    """

    request: GlobalAssistantInput
    resolution: ProjectResolution
    known_projects: list[tuple[str, str]]
    messages: list
    history: list[dict[str, str]]
    replay: GlobalAssistantOutcome | None = None
    # Preserve the customer's original question through a picker selection;
    # never persist this effective query as the user's message text.
    effective_query: str | None = None

    def __post_init__(self) -> None:
        if self.effective_query is None:
            object.__setattr__(self, "effective_query", self.request.query)

    def clarification_candidates(self) -> tuple[str, ...]:
        """Picker options for an unresolved/ambiguous turn surfaced to the client.

        AMBIGUOUS carries the resolver's matched keys (the disambiguation set);
        UNRESOLVED has no deterministic match, so the live catalogue is offered
        instead. Fail-closed: a degraded/empty catalogue yields ``()`` and the
        generic "which project?" prompt. Both the SSE ``project_context`` frame
        and the core's unresolved answer read this single source of truth so the
        picker never drifts between the two surfaces.
        """
        return (
            tuple(self.resolution.candidates)
            if self.resolution.candidates
            else tuple(key for key, _ in self.known_projects)
        )


class GlobalAssistantService:
    """Coordinates resolution, server-side history, retrieval, and storage.

    Dependencies are injected so transports do not leak into this application
    service and tests can prove resolver-before-retrieval without a database.
    """

    def __init__(
        self,
        *,
        history: ChatHistoryService,
        pipeline_factory: Callable[..., Any],
        catalogue_loader: Callable[..., Awaitable[list[tuple[str, str]]]] = _load_live_catalogue,
    ) -> None:
        self._history = history
        self._pipeline_factory = pipeline_factory
        self._catalogue_loader = catalogue_loader

    async def _emit(self, callback: EventCallback | None, event: str, data: dict[str, Any]) -> None:
        if callback is None:
            return
        result = callback(event, data)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _server_history(messages, project_key: str) -> list[dict[str, str]]:
        """Build bounded retrieval history from OWNED global messages.

        Only neutral turns (``project_key`` NULL) plus turns whose project_key
        equals the resolved project are eligible; other projects' turns are
        never leaked into this retrieval scope.

        Ownership is filtered BEFORE bounding, so an eligible message older
        than the newest N total messages is never discarded by a pre-filter
        window of foreign-project turns (which would leak scope / drop a
        legitimate Camellia turn when the newest window is all Soleil). The
        bound (latest 8 eligible) is applied to the filtered eligible set only.
        """
        eligible: list[dict[str, str]] = []
        for message in messages:
            if message.project_key not in (None, project_key):
                continue
            if message.role in ("user", "assistant") and message.content:
                eligible.append({"role": message.role, "content": message.content})
        # Latest 8 eligible messages (oldest dropped first, never foreign ones).
        return eligible[-8:]

    def _find_replay(
        self, messages: list, source_event_key: str
    ) -> GlobalAssistantOutcome | None:
        """Recover a prior visible outcome keyed by a private source_event_key.

        Scans the ALREADY-LOADED message list (the caller's single ``global_messages``
        read) — never issues a second DB read.

        Persisted envelope layout (identical for resolved and unresolved turns):

            assistant_meta = {"ga_outcome": {<serialised GlobalAssistantOutcome.as_dict()>}, ...}

        The private key is stripped before reconstruction so it cannot leak.
        A successful replay is a NO-RAG turn: no pipeline ran, so ``rag_ran``
        is forced False regardless of what the prior persisted outcome carried.
        """
        for message in reversed(messages):
            if message.role != "assistant" or not message.meta:
                continue
            if message.meta.get(_SOURCE_EVENT_META_KEY) != source_event_key:
                continue
            envelope = message.meta.get("ga_outcome")
            if not isinstance(envelope, dict):
                return None
            return GlobalAssistantOutcome(
                answer=envelope.get("answer", message.content),
                resolution_status=envelope.get("resolution_status", "replayed"),
                resolution_source=envelope.get("resolution_source", "source_event"),
                candidates=tuple(envelope.get("candidates", []) or ()),
                project_key=envelope.get("project_key"),
                meta={
                    key: value
                    for key, value in envelope.get("meta", {}).items()
                    if key != _SOURCE_EVENT_META_KEY
                },
                rag_ran=False,
                replayed=True,
                trace_id=envelope.get("trace_id", ""),
                latency_ms=envelope.get("latency_ms", 0),
            )
        return None

    async def prepare_turn(self, request: GlobalAssistantInput) -> PreparedTurn:
        """Resolve project, build server-side history, and check replay.

        Does NOT run the pipeline or persist. The returned ``PreparedTurn`` is
        the seam at which the HTTP adapter may reserve quota strictly after
        resolution. If a replay was matched, ``prepared.replay`` is set and
        the adapter must return it without reservation or persistence.
        """
        session = await self._history.global_session(
            session_id=request.session_id,
            device_id=request.device_id,
            identity_key=request.identity_key,
        )
        active_project = session.active_project_key if session else None
        messages = await self._history.global_messages(
            session_id=request.session_id,
            device_id=request.device_id,
            identity_key=request.identity_key,
        )

        # Idempotency: source-event replay MUST happen BEFORE resolution/RAG.
        # Reuse the already-loaded message list — no second DB read.
        replay = None
        if request.source_event_key is not None:
            replay = self._find_replay(
                messages=messages, source_event_key=request.source_event_key
            )
        if replay is not None:
            # Replay skips resolution, pipeline, persistence, and quota.
            return PreparedTurn(
                request=request,
                resolution=_noop_resolution(),
                known_projects=[],
                messages=messages,
                history=[],
                replay=replay,
            )

        try:
            known_projects = await self._catalogue_loader()
        except Exception:  # noqa: BLE001 — fail closed: empty catalogue blocks resolve/RAG
            known_projects = []
        resolution = await resolve_global_project(
            query=request.query,
            selected_project_key=request.selected_project_key,
            route_project_key=request.route_project_key,
            session_project_key=active_project,
            known_projects=known_projects,
        )

        # Server-side history: bounded, neutral + resolved project only.
        project_key = resolution.project_key or ""
        history = (
            self._server_history(messages, project_key)
            if resolution.status is ProjectResolutionStatus.RESOLVED
            else []
        )

        effective_query = request.query
        if resolution.source is ProjectResolutionSource.USER_SELECTION:
            intent = _pending_neutral_intent(messages)
            if intent and intent != request.query:
                effective_query = f"{intent}\n{request.query}"

        return PreparedTurn(
            request=request,
            resolution=resolution,
            known_projects=known_projects,
            messages=messages,
            history=history,
            effective_query=effective_query,
        )

    async def execute_prepared_turn(self, prepared: PreparedTurn) -> GlobalAssistantOutcome:
        """Run the pipeline for a resolved turn, persist, and return the outcome.

        Quota reservation (in the adapter) occurs between ``prepare_turn`` and
        this call. Raises ``GlobalAssistantPersistenceError`` if durability fails.
        Raises ``GlobalAssistantRedirectMismatchError`` if the pipeline payload's
        ``project_redirect`` contradicts the resolved project_key (invariant).
        """
        started = time.perf_counter()

        # Unresolved: no RAG, no quota, just persist the clarification prompt.
        if prepared.resolution.status is not ProjectResolutionStatus.RESOLVED:
            # Single-source picker: resolution matches for AMBIGUOUS, else the
            # live catalogue for UNRESOLVED (fail-closed: empty catalogue -> ()).
            candidates = prepared.clarification_candidates()
            answer = (
                "Bạn muốn hỏi dự án nào? Vui lòng chọn một trong các dự án: "
                + ", ".join(candidates)
                if candidates
                else "Bạn muốn hỏi dự án nào? Vui lòng cho biết dự án."
            )
            outcome = GlobalAssistantOutcome(
                answer=answer,
                resolution_status=prepared.resolution.status.value,
                resolution_source=prepared.resolution.source.value,
                candidates=candidates,
                meta={"candidates": list(candidates)},
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            await self._persist_outcome(
                request=prepared.request, resolved_project_key=None, outcome=outcome
            )
            return outcome

        project_key = prepared.resolution.project_key
        pipeline = self._pipeline_factory(on_event=prepared.request.on_event)
        payload = await pipeline.run(
            prepared.effective_query,
            prepared.request.session_id,
            None,
            prepared.history,
            project_key=project_key,
            device_id=prepared.request.device_id,
            on_event=prepared.request.on_event,
            known_projects=prepared.known_projects,
        )

        # Invariant: pipeline payload's project_redirect must agree with the
        # resolved project_key. project_redirect is a structured dict with a
        # ``project_key`` field (see build_redirect); compare the field, and
        # reject malformed shapes too (fail closed without persisting).
        redirect = payload.get("project_redirect")
        if redirect is not None:
            if not isinstance(redirect, dict) or "project_key" not in redirect:
                raise GlobalAssistantRedirectMismatchError(
                    resolved=project_key, redirected="<malformed>"
                )
            if redirect.get("project_key") != project_key:
                raise GlobalAssistantRedirectMismatchError(
                    resolved=project_key, redirected=redirect.get("project_key")
                )

        answer = str(payload.get("answer") or "")
        meta = {key: payload[key] for key in ("sources", "facts", "images", "places", "routing") if key in payload}
        outcome = GlobalAssistantOutcome(
            answer=answer,
            resolution_status=prepared.resolution.status.value,
            resolution_source=prepared.resolution.source.value,
            project_key=project_key,
            meta=meta,
            rag_ran=True,
            trace_id=str(payload.get("trace_id") or ""),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        await self._persist_outcome(
            request=prepared.request, resolved_project_key=project_key, outcome=outcome
        )
        return outcome

    async def run(self, request: GlobalAssistantInput) -> GlobalAssistantOutcome:
        """Convenience wrapper: prepare → execute. For non-SSE transports.

        The HTTP adapter should use ``prepare_turn`` / ``execute_prepared_turn``
        directly so it can reserve quota strictly after resolution (spec §5.3).
        """
        prepared = await self.prepare_turn(request)
        if prepared.replay is not None:
            return prepared.replay
        return await self.execute_prepared_turn(prepared)

    async def _persist_outcome(
        self,
        *,
        request: GlobalAssistantInput,
        resolved_project_key: str | None,
        outcome: GlobalAssistantOutcome,
    ) -> None:
        """Centralize persistence for this core's flows only.

        The legacy /query path keeps its own project-scoped persistence; this
        service only appends to the GLOBAL session, so there is no double-write
        against legacy routes. The full outcome envelope is stored under the
        ``ga_outcome`` meta key so replays can reconstruct the public view.
        Persistence failure is surfaced as a typed error — never as success.
        """
        try:
            persisted = await self._history.persist_global_turn(
                session_id=request.session_id,
                device_id=request.device_id,
                identity_key=request.identity_key,
                resolved_project_key=resolved_project_key,
                user_content=request.query,
                assistant_content=outcome.answer,
                assistant_meta={
                    **outcome.meta,
                    "ga_outcome": outcome.as_dict(),
                    **({_SOURCE_EVENT_META_KEY: request.source_event_key} if request.source_event_key else {}),
                },
            )
        except Exception as exc:  # noqa: BLE001 — degrade to typed error, not success
            raise GlobalAssistantPersistenceError("persist_global_turn failed") from exc
        # persist_global_turn returns False on missing session/ownership or
        # degraded persistence — that is a failure, not a soft success.
        if persisted is False:
            raise GlobalAssistantPersistenceError("persist_global_turn returned False")


def _noop_resolution() -> ProjectResolution:
    """Placeholder resolution for the replay short-circuit path."""
    return ProjectResolution(
        status=ProjectResolutionStatus.AMBIGUOUS,
        project_key=None,
        source=ProjectResolutionSource.NONE,
        candidates=(),
    )


__all__ = [
    "EventCallback",
    "GlobalAssistantInput",
    "GlobalAssistantOutcome",
    "GlobalAssistantPersistenceError",
    "GlobalAssistantRedirectMismatchError",
    "PreparedTurn",
    "GlobalAssistantService",
]
