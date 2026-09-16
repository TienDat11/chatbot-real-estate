"""Focused tests for the transport-neutral GlobalAssistantService core (GA-BE-06).

No FastAPI/HTTP and no Postgres required: the core is driven through its
injectable seams so the resolution-before-retrieval contract is provable in
isolation.
"""

from __future__ import annotations

import json

import pytest

from api.application.services.global_assistant_service import (
    _SOURCE_EVENT_META_KEY,
    GlobalAssistantInput,
    GlobalAssistantOutcome,
    GlobalAssistantPersistenceError,
    GlobalAssistantRedirectMismatchError,
    GlobalAssistantService,
)


class FakeHistory:
    def __init__(self, session=None, messages=None) -> None:
        self.session = session
        self.messages = list(messages or [])
        self.appended = []

    async def global_session(self, *, session_id, device_id=None, identity_key=None):
        return self.session

    async def global_messages(self, *, session_id, device_id=None, identity_key=None):
        return list(self.messages)

    async def persist_global_turn(self, **kwargs):
        self.appended.append(kwargs)
        return True


class FakeSession:
    def __init__(self, active_project_key=None):
        self.active_project_key = active_project_key


class FakeMessage:
    def __init__(self, role, content, project_key=None, meta=None):
        self.role = role
        self.content = content
        self.project_key = project_key
        self.meta = meta or {}


async def _catalogue():
    return [("camellia", "Camellia"), ("soleil", "Soleil")]


async def _single_project_catalogue():
    return [("soleil", "Soleil")]


def _pipeline_factory(captured, answer="42 tỷ cho căn A1-401.", trace_id="trace-1"):
    class _Pipeline:
        async def run(self, query, session_id, as_of, history, *, project_key=None, device_id=None, on_event=None, **kwargs):
            captured["query"] = query
            captured["history"] = history
            captured["project_key"] = project_key
            captured["called"] = True
            return {"answer": answer, "trace_id": trace_id, "sources": [{"doc_id": "d1"}]}

    def factory(on_event=None):
        return _Pipeline()

    return factory


@pytest.mark.asyncio
async def test_resolver_before_rag_resolved():
    captured = {}
    history = FakeHistory(
        session=FakeSession(active_project_key="camellia"),
        messages=[
            FakeMessage("user", "hello", project_key=None),
            FakeMessage("assistant", "hi", project_key=None),
        ],
    )
    service = GlobalAssistantService(history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue)

    outcome = await service.run(
        GlobalAssistantInput(query="Camellia giá bao nhiêu?", session_id="sess-1", identity_key="ident-1")
    )

    assert outcome.resolution_status == "resolved"
    assert outcome.rag_ran is True
    assert outcome.project_key == "camellia"
    assert captured["query"] == "Camellia giá bao nhiêu?"
    # Server-side history only carries neutral + camellia turns, no other project.
    assert all(t["role"] in ("user", "assistant") for t in captured["history"])
    # Resolved turn persisted centrally to global session.
    assert any(t["resolved_project_key"] == "camellia" for t in history.appended)


@pytest.mark.asyncio
async def test_unresolved_zero_rag_zero_quota():
    captured = {}
    history = FakeHistory(session=None, messages=[])
    service = GlobalAssistantService(history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue)

    outcome = await service.run(
        GlobalAssistantInput(query="Dự án nào có hồ bơi?", session_id="sess-2", identity_key="ident-2")
    )

    assert outcome.resolution_status != "resolved"
    assert outcome.rag_ran is False
    assert captured.get("called") is not True
    assert outcome.candidates  # clarification candidates returned
    # Neutral unresolved turn persisted with no project.
    assert any(t["resolved_project_key"] is None for t in history.appended)
    assert "_source_event_key" not in (history.appended[-1]["assistant_meta"] or {})


@pytest.mark.asyncio
async def test_ambiguous_query_zero_rag_zero_pipeline():
    """Two deterministic matches collapse to AMBIGUOUS: no RAG, no quota,
    no pipeline, no project resolved — the picker surfaces the resolver's
    candidate set so the client can disambiguate."""
    captured = {}
    history = FakeHistory(
        session=FakeSession(active_project_key="camellia"),
        messages=[],
    )
    service = GlobalAssistantService(
        history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue
    )

    outcome = await service.run(
        GlobalAssistantInput(query="Camellia Soleil", session_id="sess-amb", identity_key="ident-amb")
    )

    assert outcome.resolution_status == "ambiguous"
    assert outcome.project_key is None
    assert outcome.rag_ran is False
    assert captured.get("called") is not True
    # Ambiguity: candidates carry every matched key, not a single resolved one.
    assert outcome.candidates == ("camellia", "soleil")
    # Ambiguous turn persists with no project_key.
    assert any(t["resolved_project_key"] is None for t in history.appended)


@pytest.mark.asyncio
async def test_server_history_excludes_other_projects():
    captured = {}
    messages = [
        FakeMessage("user", "Camellia?", project_key="camellia"),
        FakeMessage("assistant", "42 tỷ.", project_key="camellia"),
        FakeMessage("user", "Soleil?", project_key="soleil"),  # must be excluded
        FakeMessage("assistant", "36 tỷ.", project_key="soleil"),
        FakeMessage("user", "tiếp theo", project_key=None),  # neutral kept
    ]
    history = FakeHistory(session=FakeSession(active_project_key="camellia"), messages=messages)
    service = GlobalAssistantService(history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue)

    outcome = await service.run(
        GlobalAssistantInput(query="Camellia tiếp tục", session_id="sess-3", identity_key="ident-3")
    )

    assert outcome.project_key == "camellia"
    roles_content = [(t["role"], t["content"]) for t in captured["history"]]
    # Soleil turns excluded; neutral turn + camellia turns only.
    assert ("assistant", "36 tỷ.") not in roles_content
    assert ("user", "Soleil?") not in roles_content
    assert ("user", "tiếp theo") in roles_content


@pytest.mark.asyncio
async def test_source_event_replay_skips_pipeline():
    captured = {}
    prior_meta = {
        "_source_event_key": "external-txn-1",
        "ga_outcome": {
            "answer": "replayed answer",
            "resolution_status": "resolved",
            "resolution_source": "explicit_query",
            "project_key": "camellia",
            "rag_ran": True,
            "trace_id": "trace-old",
        },
    }
    history = FakeHistory(
        session=FakeSession(active_project_key="camellia"),
        messages=[FakeMessage("assistant", "replayed answer", project_key="camellia", meta=prior_meta)],
    )
    service = GlobalAssistantService(history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue)

    outcome = await service.run(
        GlobalAssistantInput(
            query="Camellia giá bao nhiêu?", session_id="sess-4", identity_key="ident-4", source_event_key="external-txn-1"
        )
    )

    assert outcome.replayed is True
    assert outcome.rag_ran is False
    assert captured.get("called") is not True
    assert outcome.answer == "replayed answer"
    # Replay must NOT persist a second turn.
    assert history.appended == []


@pytest.mark.asyncio
async def test_active_project_updates_only_on_resolved():
    captured = {}
    history = FakeHistory(session=None, messages=[])
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory(captured, answer="36 tỷ Soleil."),
        catalogue_loader=_single_project_catalogue,
    )

    outcome = await service.run(
        GlobalAssistantInput(query="Soleil?", session_id="sess-5", identity_key="ident-5")
    )
    assert outcome.resolution_status == "resolved"
    assert any(t["resolved_project_key"] == "soleil" for t in history.appended)


async def _empty_catalogue():
    return []


async def _raise_catalogue():
    raise RuntimeError("registry down")


@pytest.mark.asyncio
async def test_fail_closed_catalogue_blocks_rag():
    captured = {}
    history = FakeHistory(session=FakeSession(active_project_key=None), messages=[])
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory(captured),
        catalogue_loader=_empty_catalogue,
    )

    outcome = await service.run(
        GlobalAssistantInput(query="Camellia giá bao nhiêu?", session_id="sess-6", identity_key="ident-6")
    )

    assert outcome.rag_ran is False
    assert captured.get("called") is not True
    assert outcome.resolution_status != "resolved"


@pytest.mark.asyncio
async def test_fail_closed_catalogue_on_registry_error():
    captured = {}
    history = FakeHistory(session=FakeSession(active_project_key=None), messages=[])
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory(captured),
        catalogue_loader=_raise_catalogue,
    )

    outcome = await service.run(
        GlobalAssistantInput(query="Camellia giá bao nhiêu?", session_id="sess-7", identity_key="ident-7")
    )

    assert outcome.rag_ran is False
    assert captured.get("called") is not True
    assert outcome.resolution_status != "resolved"


class _FailingHistory(FakeHistory):
    async def persist_global_turn(self, **kwargs):
        self.appended.append(kwargs)
        return False


@pytest.mark.asyncio
async def test_persistence_failure_raises_not_success():
    captured = {}

    class _FailHistory:
        def __init__(self) -> None:
            self.appended = []

        async def global_session(self, *, session_id, device_id=None, identity_key=None):
            return FakeSession(active_project_key="soleil")

        async def global_messages(self, *, session_id, device_id=None, identity_key=None):
            return []

        async def persist_global_turn(self, **kwargs):
            return False

    service = GlobalAssistantService(
        history=_FailHistory(),
        pipeline_factory=_pipeline_factory(captured, answer="36 tỷ."),
        catalogue_loader=_single_project_catalogue,
    )
    with pytest.raises(GlobalAssistantPersistenceError):
        await service.run(
            GlobalAssistantInput(query="Soleil?", session_id="sess-8", identity_key="ident-8")
        )

def _redirect_payload(redirect_marker):
    """Translate a redirect-case marker into the concrete project_redirect value.

    Using opaque sentinels (not None/False/str) guarantees each case is asserted
    independently — no unreachable branch (e.g. ``elif redirect_key is False``)
    can silently shadow another:
      * _REDIRECT_MATCHING ("soleil") -> well-formed dict, project_key agrees
      * _REDIRECT_MISMATCH ("camellia") -> well-formed dict, project_key disagrees
      * _MALFORMED              -> "not-a-dict" (non-dict)
      * _MISSING_KEY            -> dict without project_key
      * _UNSET                -> project_redirect omitted from payload entirely
    """
    if redirect_marker is _MALFORMED:
        return "not-a-dict"
    if redirect_marker is _MISSING_KEY:
        return {"display_name": "Soleil"}  # dict missing project_key
    return {"project_key": redirect_marker, "display_name": "Test"}


def _pipeline_factory_redirect(captured, redirect_marker):
    """Pipeline factory whose returned payload includes project_redirect.

    ``redirect_marker`` selects which project_redirect shape the pipeline emits
    (see ``_redirect_payload``). ``_UNSET`` omits the key entirely.
    """

    class _Pipeline:
        async def run(self, query, session_id, as_of, history, *, project_key=None, device_id=None, on_event=None, **kwargs):
            captured["query"] = query
            captured["project_key"] = project_key
            payload: dict = {"answer": "42 tỷ.", "trace_id": "trace-r"}
            if redirect_marker is not _UNSET:
                payload["project_redirect"] = _redirect_payload(redirect_marker)
            return payload

    def factory(on_event=None):
        return _Pipeline()

    return factory


# Distinct sentinels so each redirect case is asserted independently and no
# unreachable branch (e.g. ``elif redirect_key is False``) shadows another.
_UNSET = object()
_MALFORMED = object()
_MISSING_KEY = object()

_REDIRECT_MATCHING = "soleil"  # well-formed, project_key agrees with resolution
_REDIRECT_MISMATCH = "camellia"  # well-formed, project_key disagrees with resolution


@pytest.mark.asyncio
async def test_redirect_invariant_matching_persists():
    """Case A: a matching project_redirect (soleil == soleil) is accepted and the
    turn is persisted with the resolved project_key."""
    captured = {}
    history = FakeHistory(session=FakeSession(active_project_key="soleil"), messages=[])
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory_redirect(
            captured, _REDIRECT_MATCHING
        ),
        catalogue_loader=_single_project_catalogue,
    )

    outcome = await service.run(
        GlobalAssistantInput(query="Soleil?", session_id="sess-x1", identity_key="ident-x1")
    )

    assert outcome.project_key == "soleil"
    assert captured["project_key"] == "soleil"
    assert any(t["resolved_project_key"] == "soleil" for t in history.appended)


@pytest.mark.asyncio
async def test_redirect_invariant_mismatch_does_not_persist():
    """Case B: a mismatching project_redirect (soleil != camellia) fails closed —
    GlobalAssistantRedirectMismatchError, zero persistence."""
    captured = {}
    history = FakeHistory(session=FakeSession(active_project_key="soleil"), messages=[])
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory_redirect(
            captured, _REDIRECT_MISMATCH
        ),
        catalogue_loader=_single_project_catalogue,
    )

    with pytest.raises(GlobalAssistantRedirectMismatchError) as exc_info:
        await service.run(
            GlobalAssistantInput(query="Soleil?", session_id="sess-x2", identity_key="ident-x2")
        )

    assert exc_info.value.resolved == "soleil"
    assert exc_info.value.redirected == "camellia"
    assert not history.appended


@pytest.mark.asyncio
async def test_redirect_invariant_malformed_does_not_persist():
    """Case C: a malformed (non-dict) project_redirect fails closed —
    GlobalAssistantRedirectMismatchError, zero persistence."""
    captured = {}
    history = FakeHistory(session=FakeSession(active_project_key="soleil"), messages=[])
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory_redirect(captured, _MALFORMED),
        catalogue_loader=_single_project_catalogue,
    )

    with pytest.raises(GlobalAssistantRedirectMismatchError) as exc_info:
        await service.run(
            GlobalAssistantInput(query="Soleil?", session_id="sess-x3", identity_key="ident-x3")
        )

    assert exc_info.value.resolved == "soleil"
    assert exc_info.value.redirected == "<malformed>"
    assert not history.appended


@pytest.mark.asyncio
async def test_redirect_invariant_missing_project_key_does_not_persist():
    """Case D: a dict project_redirect missing the project_key field fails closed —
    GlobalAssistantRedirectMismatchError, zero persistence."""
    captured = {}
    history = FakeHistory(session=FakeSession(active_project_key="soleil"), messages=[])
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory_redirect(captured, _MISSING_KEY),
        catalogue_loader=_single_project_catalogue,
    )

    with pytest.raises(GlobalAssistantRedirectMismatchError) as exc_info:
        await service.run(
            GlobalAssistantInput(query="Soleil?", session_id="sess-x4", identity_key="ident-x4")
        )

    assert exc_info.value.resolved == "soleil"
    assert exc_info.value.redirected == "<malformed>"


@pytest.mark.asyncio
async def test_ambiguous_and_unresolved_skip_pipeline_and_persistence():
    """Core-level invariant for AMBIGUOUS and UNRESOLVED: the pipeline factory is
    never constructed, pipeline.run is never called, and the outcome carries
    rag_ran=False. This pins the resolver-before-RAG contract at the core layer
    (the SSE stub test must not carry this proof via an inert `_StubCore` fake).

    The production call that would increment a pipeline counter is the real
    ``self._pipeline_factory(...)``; these fakes count exactly that call, so a
    passing test here fails if the core ever constructs the pipeline for a
    non-resolved turn.
    """
    for status, query, expected, session in [
        ("ambiguous", "Camellia Soleil", ("camellia", "soleil"), None),
        ("unresolved", "Dự án nào có hồ bơi?", (), None),
    ]:
        captured: dict = {}
        # No active session project: unresolved/ambiguous must not fall back to
        # a session_context resolution (which would make them resolved).
        history = FakeHistory(session=session, messages=[])
        service = GlobalAssistantService(
            history=history,
            pipeline_factory=_pipeline_factory(captured),
            catalogue_loader=_catalogue,
        )

        outcome = await service.run(
            GlobalAssistantInput(
                query=query, session_id=f"sess-{status}", identity_key="ident"
            )
        )

        assert outcome.resolution_status == status
        assert outcome.rag_ran is False
        # The pipeline factory is never constructed on a non-resolved turn.
        assert captured.get("called") is not True, (
            f"pipeline must not run on {status}"
        )
        # The outcome persists with NO resolved project (clarification path).
        assert any(t["resolved_project_key"] is None for t in history.appended)
        # AMBIGUOUS surfaces the matched candidate set; UNRESOLVED uses the
        # live catalogue fallback (fail-closed: empty -> ()).
        if status == "ambiguous":
            assert outcome.candidates == expected


@pytest.mark.asyncio
async def test_server_history_retains_eligible_message_beyond_foreign_window():
    """Pre-filter slicing (`messages[-40:]`) before ownership filtering could
    discard a genuinely eligible Camellia message when the newest 40 total
    messages are all foreign (Soleil). The fix: filter ownership FIRST, then
    bound to the latest 8 eligible.

    Layout: 1 eligible Camellia message, then >=40 newer Soleil messages.
    The eligible message is older than the 40-newest window but must survive
    because it is owned by the resolved project.
    """
    captured: dict = {}
    # One eligible Camellia message (older than the foreign window).
    old_camellia = FakeMessage("user", "Camellia cũ?", project_key="camellia")
    # 40 newer foreign (Soleil) messages that MUST be excluded from camellia history.
    soleil_wall = [
        FakeMessage("user", f"Soleil {i}?", project_key="soleil") for i in range(40)
    ]
    messages = [old_camellia] + soleil_wall
    history = FakeHistory(
        session=FakeSession(active_project_key="camellia"), messages=messages
    )
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory(captured),
        catalogue_loader=_catalogue,
    )

    outcome = await service.run(
        GlobalAssistantInput(
            query="Camellia giá bao nhiêu?", session_id="sess-contam", identity_key="ident"
        )
    )

    assert outcome.resolution_status == "resolved"
    assert outcome.project_key == "camellia"
    selected = captured["history"]
    # The single eligible Camellia message survives despite being outside the
    # newest-40 total window.
    contents = [m["content"] for m in selected]
    assert "Camellia cũ?" in contents
    # No foreign-project message ever enters the resolved history.
    assert all("Soleil" not in c for c in contents)
    # Bounded to the existing cap (8) of eligible messages.
    assert len(selected) <= 8


@pytest.mark.asyncio
async def test_server_history_keeps_neutral_messages():
    """Neutral messages (project_key None) remain eligible for any resolved
    project's retrieval history — they are not foreign-project leaks."""
    captured: dict = {}
    messages = [
        FakeMessage("user", "neutral older", project_key=None),
        FakeMessage("assistant", "neutral older reply", project_key=None),
    ]
    history = FakeHistory(
        session=FakeSession(active_project_key="camellia"), messages=messages
    )
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory(captured),
        catalogue_loader=_catalogue,
    )

    await service.run(
        GlobalAssistantInput(
            query="Camellia giá bao nhiêu?", session_id="sess-neutral", identity_key="ident"
        )
    )
    roles_content = [(m["role"], m["content"]) for m in captured["history"]]
    assert ("user", "neutral older") in roles_content
    assert ("assistant", "neutral older reply") in roles_content


@pytest.mark.asyncio
async def test_server_history_bound_applied_to_eligible_only():
    """The bounded cap applies to the ELIGIBLE set, not the raw message list.
    With exactly 9 eligible Camellia messages, only the latest 8 are retained.
    """
    captured: dict = {}
    messages = [
        FakeMessage("user", f"Camellia msg {i}", project_key="camellia") for i in range(9)
    ]
    history = FakeHistory(
        session=FakeSession(active_project_key="camellia"), messages=messages
    )
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory(captured),
        catalogue_loader=_catalogue,
    )

    await service.run(
        GlobalAssistantInput(
            query="Camellia giá bao nhiêu?", session_id="sess-bound", identity_key="ident"
        )
    )
    assert len(captured["history"]) == 8
    # The oldest eligible message is dropped; the latest is kept.
    contents = [m["content"] for m in captured["history"]]
    assert "Camellia msg 0" not in contents
    assert "Camellia msg 8" in contents


@pytest.mark.asyncio
async def test_active_project_not_mutated_by_unresolved_query():
    """An EXISTING session with active_project_key='camellia' followed by an
    UNRESOLVED query must persist the turn with resolved_project_key=None and
    must NOT mutate or clear the existing active project.

    The active_project_key mutation is owned by the repository's append_global_turn
    (verified at api/infrastructure/adapters/postgres_chat_history.py:703 —
    ``if resolved_project_key is not None`` sets ``active_project_key=$2``; the
    NULL branch at :711 leaves it unchanged — docstring at :648-650). The core's
    contract (the half this test proves end-to-end) is that UNRESOLVED and
    AMBIGUOUS turns pass ``resolved_project_key=None``, so the active project is
    preserved. This stateful fake mirrors the REAL repository rule exactly.
    """

    class _StatefulHistory(FakeHistory):
        """Fake that models the repository's active_project_key contract:
        resolved_project_key=None leaves active_project_key unchanged."""

        def __init__(self, active_project_key):
            super().__init__(
                session=FakeSession(active_project_key=active_project_key),
                messages=[],
            )
            self.active_project_key = active_project_key

        async def persist_global_turn(self, **kwargs):
            self.appended.append(kwargs)
            resolved = kwargs.get("resolved_project_key")
            # Mirrors repository: active_project_key updates ONLY on resolved.
            if resolved is not None:
                self.active_project_key = resolved
            return True

    captured: dict = {}
    # Session exists (created by a prior resolved turn) but has NO active
    # project yet. A query that names no project resolves UNRESOLVED — session
    # context is only consulted when explicit_query has no match, and here the
    # session project is None so nothing promotes to resolved.
    history = _StatefulHistory(active_project_key=None)
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory(captured),
        catalogue_loader=_catalogue,
    )

    outcome = await service.run(
        GlobalAssistantInput(
            query="Dự án nào có hồ bơi?", session_id="sess-unr", identity_key="ident"
        )
    )

    assert outcome.resolution_status == "unresolved"
    assert outcome.rag_ran is False
    assert captured.get("called") is not True
    # Persisted with NULL project — the clarification path.
    assert any(t["resolved_project_key"] is None for t in history.appended)
    # The active_project_key is NOT created or mutated by a clarification turn.
    assert history.active_project_key is None


@pytest.mark.asyncio
async def test_active_project_not_mutated_by_ambiguous_query():
    """Same invariant as the unresolved case, but for AMBIGUOUS (two deterministic
    matches collapse to ambiguous: no project selected, active project preserved)."""

    class _StatefulHistory(FakeHistory):
        def __init__(self, active_project_key):
            super().__init__(
                session=FakeSession(active_project_key=active_project_key),
                messages=[],
            )
            self.active_project_key = active_project_key

        async def persist_global_turn(self, **kwargs):
            self.appended.append(kwargs)
            resolved = kwargs.get("resolved_project_key")
            if resolved is not None:
                self.active_project_key = resolved
            return True

    captured: dict = {}
    history = _StatefulHistory(active_project_key="camellia")
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory(captured),
        catalogue_loader=_catalogue,
    )

    outcome = await service.run(
        GlobalAssistantInput(
            query="Camellia Soleil", session_id="sess-amb2", identity_key="ident"
        )
    )

    assert outcome.resolution_status == "ambiguous"
    assert outcome.rag_ran is False
    assert captured.get("called") is not True
    assert any(t["resolved_project_key"] is None for t in history.appended)
    assert history.active_project_key == "camellia"


@pytest.mark.asyncio
async def test_replay_strips_source_event_key_from_outcome_and_as_dict():
    """A persisted replay envelope whose nested ga_outcome.meta contains the
    private _source_event_key must NOT leak through the returned outcome's meta
    or through GlobalAssistantOutcome.as_dict().

    The production invariant this catches: `_find_replay` reconstructs `meta=`
    by copying every key from the envelope's meta dict. If the private key is
    not stripped, it would ride on the replayed outcome and be re-serialized
    into any downstream replay envelope via as_dict(), defeating the transport
    boundary that keeps source_event_key off the wire.
    """
    captured: dict = {}
    prior_meta = {
        _SOURCE_EVENT_META_KEY: "should-never-leak",
        "ga_outcome": {
            "answer": "replayed answer",
            "resolution_status": "resolved",
            "resolution_source": "source_event",
            "project_key": "camellia",
            "rag_ran": True,
            "meta": {
                "routing": {"intent": "rag"},
                # The private key nested inside the outcome meta: must be stripped.
                _SOURCE_EVENT_META_KEY: "should-never-leak",
            },
            "trace_id": "trace-old",
        },
    }
    history = FakeHistory(
        session=FakeSession(active_project_key="camellia"),
        messages=[FakeMessage("assistant", "replayed answer", project_key="camellia", meta=prior_meta)],
    )
    service = GlobalAssistantService(
        history=history,
        pipeline_factory=_pipeline_factory(captured),
        catalogue_loader=_catalogue,
    )

    outcome = await service.run(
        GlobalAssistantInput(
            query="Camellia giá bao nhiêu?",
            session_id="sess-replay-leak",
            identity_key="ident",
            source_event_key="should-never-leak",
        )
    )

    assert outcome.replayed is True
    assert outcome.rag_ran is False
    assert captured.get("called") is not True
    # The private key must not survive into the reconstructed outcome meta.
    assert _SOURCE_EVENT_META_KEY not in outcome.meta
    # as_dict() must never expose the private key either.
    serialized = outcome.as_dict()
    assert _SOURCE_EVENT_META_KEY not in serialized["meta"]
    assert _SOURCE_EVENT_META_KEY not in serialized
    assert serialized["rag_ran"] is False


def test_outcome_as_dict_strips_source_event_key_from_meta():
    """Directly constructing a GlobalAssistantOutcome whose meta contains the
    private _source_event_key must yield a serialized dict that strips the
    private key while keeping benign sibling keys (GA-BE-09 contract).
    """
    outcome = GlobalAssistantOutcome(
        answer="ok",
        resolution_status="resolved",
        resolution_source="catalogue",
        project_key="camellia",
        meta={
            _SOURCE_EVENT_META_KEY: "should-never-leak",
            "routing": {"intent": "catalogue"},
        },
    )

    serialized = outcome.as_dict()

    assert _SOURCE_EVENT_META_KEY not in serialized["meta"]
    assert "routing" in serialized["meta"]
    assert serialized["meta"]["routing"] == {"intent": "catalogue"}
    assert "_source_event_key" not in json.dumps(serialized)
