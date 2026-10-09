"""Messenger Bot processor unit tests.

DB-free; only injected fake store, core, sender and admission are used. No
provider, database, or network transport. Run with -m "not e2e".
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from api.application.ports.messenger_bot import MessengerAdmissionDecision
from api.application.services.global_assistant_service import (
    GlobalAssistantInput,
    GlobalAssistantOutcome,
)
from api.application.services.messenger_bot_identity import (
    global_session_id,
    opaque_identity,
)
from api.application.services.messenger_bot_processor import (
    MessengerBotProcessor,
    _BRAKE,
    _EMPTY_CATALOGUE,
    _GREETING,
    _INTRO_QUERY,
    _SELECT_PREFIX,
)
from api.infrastructure.adapters.postgres_messenger_events import (
    MessengerEvent,
    valid_messenger_cache,
)

CHAT = "private-chat-not-for-logs"
TOKEN = "test-provider-token-not-for-logs"
SECRET = "test-only-independent-identity-secret-01"
CATALOGUE = [("camellia", "Camellia"), ("soleil", "Soleil")]


def make_event(psid=CHAT, text="", kind="message", selected_project_key=None):
    identity_key = opaque_identity(psid, SECRET)
    source_event_key = f"messenger-bot:event:v1:1"
    return MessengerEvent(
        id=1,
        psid=psid,
        text=text,
        kind=kind,
        selected_project_key=selected_project_key,
        identity_key=identity_key,
        source_event_key=source_event_key,
        outbound_message_ids=None,
    )


class FakeStore:
    """One MessengerEvent row plus the full store API the processor calls.

    claim_event mutates the LIVE row (claim token, status, lease) exactly like
    the real store does on an optimistic UPDATE, then returns a deep copy so
    the caller can mutate its local copy without corrupting the live row. That
    leaves _owns()/check_ownership matching the production behaviour. The cache
    returned on claim lets a replay test the "cached envelope, no provider
    receipt yet" path; outbound ids surface on claim only when outbound_on_claim
    is set. The caller-supplied call_log is shared by the real test harness so
    cross-component ordering (record_selection vs core.run) is observable.
    """

    def __init__(self, call_log):
        self.event = deepcopy(make_event())
        self.events = {1: self.event}
        self.cached_outcome = None
        self.outbound_on_claim = False
        self.snapshot = None
        self.calls = []
        self.record_selection_calls = []
        self.call_log = call_log

    def _owns(self, event):
        live = self.events.get(event.id)
        return (
            live is not None
            and live.status == "processing"
            and live.claim_token == event.claim_token
            and live.lease_remaining > 0
        )

    async def claim_event(self, event_id):
        live = self.events.get(event_id)
        if live is None or live.status in ("done", "dead", "rejected"):
            return None
        live.claim_token = uuid4()
        live.status = "processing"
        live.attempt_count += 1
        live.lease_remaining = 120.0
        e = deepcopy(live)
        e.outbound_message_ids = deepcopy(live.outbound_message_ids) if self.outbound_on_claim else None
        self.calls.append(("claim_event", event_id))
        return e

    async def check_ownership(self, event, *, minimum_remaining=0):
        return (120.0 if self._owns(event) and 120.0 > minimum_remaining else None)

    async def renew_lease(self, event):
        return await self.check_ownership(event)

    async def cache_outcome(self, event, outcome):
        if not self._owns(event):
            return False
        self.cached_outcome = deepcopy(outcome)
        self.calls.append(("cache_outcome", event.id))
        return True

    async def complete(self, event, message_ids):
        if not self._owns(event):
            return False
        live = self.events[event.id]
        live.outbound_message_ids = list(message_ids)
        live.assistant_outcome = self.cached_outcome
        live.status = "done"
        live.claim_token = None
        live.completed_at = None
        self.calls.append(("complete", event.id))
        return True

    async def fail(self, event, code, *, retryable=True):
        if not self._owns(event):
            return False
        live = self.events[event.id]
        live.status = "failed" if retryable and live.attempt_count < 3 else "dead"
        live.claim_token = None
        return True

    async def reject(self, event):
        if not self._owns(event):
            return False
        live = self.events[event.id]
        live.status = "rejected"
        live.claim_token = None
        return True

    async def release(self, event):
        return await self.fail(event, "RELEASED")

    async def authorize(self, identity):
        # Binding active by default: an id is enough to grant access.
        return 73

    async def latest_picker_snapshot(self, identity_key):
        return self.snapshot

    async def record_selection(self, event, project_key):
        if not self._owns(event):
            return False
        self.calls.append(("record_selection", event.id, project_key))
        self.call_log.append(("record_selection", project_key))
        self.record_selection_calls.append(project_key)
        live = self.events[event.id]
        live.selected_project_key = project_key
        return True


class FakeCore:
    """Canned GlobalAssistant facade that records every GlobalAssistantInput it receives.

    The real GlobalAssistantService is heavy; duck-typing against its run(request)
    contract lets the processor drive a pure-fake core the way the Zalo tests do.
    """

    def __init__(self, call_log):
        self.requests = []
        self.call_log = call_log

    async def run(self, request):
        self.call_log.append(("core.run",))
        self.requests.append(request)
        return GlobalAssistantOutcome(
            answer="Canned project description for " + (request.selected_project_key or ""),
            resolution_status="resolved",
            resolution_source="project",
            candidates=(request.selected_project_key or "x",),
            project_key=request.selected_project_key,
        )


class FakeSender:
    """Send API facade recording every outbound call for delivery assertions."""

    def __init__(self):
        self.calls = []

    async def send_text(self, psid, text):
        self.calls.append(("send_text", psid, text))
        return f"mid.text.{len(self.calls)}"

    async def send_quick_replies(self, psid, text, options):
        self.calls.append(("send_quick_replies", psid, text, options))
        return f"mid.qr.{len(self.calls)}"


class FakeAdmission:
    """Deterministic admission decision; replayed decisions mirror the real
    started/braked vs already_started/already_braked behaviour."""

    def __init__(self, decision=MessengerAdmissionDecision.STARTED):
        self.decision = decision
        self.calls = []

    async def __call__(self, identity_key, source_event_key, *, event_id, claim_token):
        self.calls.append((identity_key, source_event_key, event_id))
        return self.decision


def _reset_event(rig, kind="message", text="", selected_project_key=None):
    e = make_event(kind=kind, text=text, selected_project_key=selected_project_key)
    rig.store.event = e
    rig.store.events[1] = deepcopy(e)
    rig.store.cached_outcome = None
    rig.store.outbound_on_claim = False
    rig.store.calls = []
    rig.core.requests = []
    rig.sender.calls = []
    rig.store.record_selection_calls = []
    rig.call_log.clear()


@pytest.fixture
def rig(monkeypatch):
    call_log = []
    store = FakeStore(call_log)
    core = FakeCore(call_log)
    sender = FakeSender()
    admission = FakeAdmission(MessengerAdmissionDecision.STARTED)
    catalogue = AsyncMock(return_value=CATALOGUE)
    settings = SimpleNamespace(
        messenger_bot_enabled=True,
        messenger_open_demo_access=False,
        messenger_core_timeout_seconds=90,
        messenger_lease_seconds=120,
        messenger_lease_margin_seconds=15,
    )
    processor = MessengerBotProcessor(
        store=store, core=core, sender=sender,
        identity_secret=SECRET, admit=admission, settings=settings,
        catalogue_loader=catalogue,
    )
    yield SimpleNamespace(store=store, core=core, sender=sender,
                          admission=admission, catalogue=catalogue, settings=settings,
                          processor=processor, call_log=call_log)


def _run(rig):
    return asyncio.run(rig.processor.process(1))


def test_numeric_reply_resolves_to_stored_picker_snapshot_order(rig):
    """Numeric reply must resolve against the picker snapshot the user saw, not
    the current catalogue index. Snapshot order differs from catalogue order,
    so text "1" resolves to the snapshot's first key ("soleil") even though
    catalogue[0] is "camellia"."""
    _reset_event(rig, kind="message", text="1")
    rig.store.snapshot = ["soleil", "camellia", "alpha"]
    assert _run(rig) == "done"
    request = rig.core.requests[0]
    # Snapshot order wins over catalogue index.
    assert request.selected_project_key == "soleil"
    assert request.query == _INTRO_QUERY
    assert request.resume_pending_intent is False
    # Selection persisted for replay determinism.
    assert rig.store.record_selection_calls == ["soleil"]
    # Intro delivery carries the change-project quick reply only.
    kind, _psid, _text, options = rig.sender.calls[0]
    assert kind == "send_quick_replies"
    assert options == [("Đổi dự án", "SELECT_PROJECT_MENU")]
    assert rig.store.calls[-1] == ("complete", 1)


def test_numeric_snapshot_path_sets_resume_pending_intent_false(rig):
    """Numeric reply on a snapshot: resume_pending_intent False, query is the
    intro query, selected key is the snapshot entry."""
    _reset_event(rig, kind="message", text="1")
    rig.store.snapshot = ["camellia", "soleil"]
    assert _run(rig) == "done"
    request = rig.core.requests[0]
    assert request.selected_project_key == "camellia"
    assert request.query == _INTRO_QUERY
    assert request.resume_pending_intent is False
    assert rig.store.record_selection_calls == ["camellia"]


def test_brake_persists_sends_and_replays_cached(rig):
    """BRAKED admission caches the brake envelope, sends the brake text once,
    never calls core, then replays the same envelope from cache on retry."""
    _reset_event(rig)
    rig.admission.decision = MessengerAdmissionDecision.BRAKED
    assert _run(rig) == "done"
    cached = rig.store.events[1].assistant_outcome
    assert valid_messenger_cache(cached) is True
    assert cached["reply_kind"] == "brake"
    assert cached["resolution_status"] == "technical_brake"
    assert cached["text"] == _BRAKE
    assert cached["picker_keys"] is None
    assert rig.sender.calls == [("send_text", CHAT, _BRAKE)]
    assert rig.core.requests == []
    assert len(rig.admission.calls) == 1
    # Replay: cache persists but no committed provider receipt yet.
    rig.store.events[1].status = "failed"
    assert _run(rig) == "done"
    assert rig.sender.calls == [("send_text", CHAT, _BRAKE), ("send_text", CHAT, _BRAKE)]
    assert rig.core.requests == []
    # Cached reply skips admission entirely.
    assert len(rig.admission.calls) == 1


def test_select_project_path_sets_resume_pending_intent_false(rig):
    """select_project event resolves an active project; resume_pending_intent is
    False and the query is the intro query, not the user's raw input."""
    _reset_event(rig, kind="select_project", selected_project_key="camellia")
    assert _run(rig) == "done"
    request = rig.core.requests[0]
    assert request.selected_project_key == "camellia"
    assert request.query == _INTRO_QUERY
    assert request.resume_pending_intent is False
    # Picker refresh with the change-project option.
    kind, _psid, _text, options = rig.sender.calls[0]
    assert kind == "send_quick_replies"
    assert options == [("Đổi dự án", "SELECT_PROJECT_MENU")]
    assert rig.store.record_selection_calls == ["camellia"]


def test_exact_name_path_sets_resume_pending_intent_false(rig):
    """Exact project-name reply resolves by name match; same resume/query/selection
    invariants as the numeric path."""
    _reset_event(rig, kind="message", text="Camellia")
    assert _run(rig) == "done"
    request = rig.core.requests[0]
    assert request.selected_project_key == "camellia"
    assert request.query == _INTRO_QUERY
    assert request.resume_pending_intent is False
    assert rig.store.record_selection_calls == ["camellia"]


def test_resume_pending_intent_dataclass_default_is_true(rig):
    """The dataclass default is unchanged: callers not passing the field (web /
    Zalo) rely on resume_pending_intent being True by default."""
    input_default = GlobalAssistantInput(
        query="test",
        session_id=global_session_id("identity"),
        identity_key="identity",
    )
    assert input_default.resume_pending_intent is True


def test_empty_catalogue_returns_plain_answer(rig):
    """Empty catalogue: a plain text answer, no quick replies and no empty
    quick_replies array."""
    _reset_event(rig, kind="get_started", text="")
    rig.catalogue.return_value = []
    assert _run(rig) == "done"
    assert rig.core.requests == []
    assert rig.store.record_selection_calls == []
    assert rig.sender.calls == [("send_text", CHAT, _EMPTY_CATALOGUE)]
    assert rig.store.events[1].assistant_outcome["reply_kind"] == "answer"
    assert rig.sender.calls[0][2] == _EMPTY_CATALOGUE
    # No quick replies of any kind.
    assert all(c[0] != "send_quick_replies" for c in rig.sender.calls)


def test_one_project_catalogue_sends_single_quick_reply(rig):
    """One-project catalogue: send_quick_replies called with exactly one option."""
    _reset_event(rig, kind="get_started", text="")
    rig.catalogue.return_value = [("camellia", "Camellia")]
    assert _run(rig) == "done"
    assert rig.core.requests == []
    kind, _psid, text, options = rig.sender.calls[0]
    assert kind == "send_quick_replies"
    assert len(options) == 1
    assert text == _GREETING.format(n=1)
    assert options[0] == ("Camellia", "SELECT_PROJECT:camellia")
    assert rig.store.events[1].assistant_outcome["reply_kind"] == "quick_replies"


def test_get_started_passes_through_admission(rig):
    """get_started has no admission exemption: admit IS called before
    _dispatch_kind, and the greeting path never reaches core."""
    _reset_event(rig, kind="get_started", text="")
    assert _run(rig) == "done"
    assert len(rig.admission.calls) == 1
    assert rig.core.requests == []
    assert rig.sender.calls[0][0] == "send_quick_replies"
    assert rig.store.events[1].status == "done"
    assert rig.store.events[1].assistant_outcome["resolution_status"] == "unresolved"


def test_stale_select_project_key_refreshes_picker_without_core(rig):
    """Stale select_project key not in the current catalogue refreshes the picker:
    picker rebuilt, core not called, no selection persisted so no project state
    change lands."""
    _reset_event(rig, kind="select_project", selected_project_key="dead-project")
    assert _run(rig) == "done"
    assert rig.core.requests == []
    assert rig.store.record_selection_calls == []
    kind, _psid, _text, options = rig.sender.calls[0]
    assert kind == "send_quick_replies"
    # Picker rebuilt from the live catalogue, not from a stale key.
    assert [opt[1] for opt in options] == ["SELECT_PROJECT:camellia", "SELECT_PROJECT:soleil"]


def test_record_selection_committed_before_core_run(rig):
    """The resolved key is persisted (record_selection) before core.run on the
    intro path, which is what makes the replay re-derive the same project."""
    _reset_event(rig, kind="message", text="Camellia")
    assert _run(rig) == "done"
    log = rig.call_log
    selection = next(i for i, c in enumerate(log) if c[0] == "record_selection")
    core_call = next(i for i, c in enumerate(log) if c[0] == "core.run")
    assert selection < core_call, "selection must be committed before the core request"
    assert log[core_call] == ("core.run",)
