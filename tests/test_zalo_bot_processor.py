"""ZB-02 transport boundary with real shared-core replay and project history.

Only injected memory stores/pipelines are used; no provider, DB, or factory choice.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.application.services import anon_identity, lead_service, quota_service
from api.application.services.global_assistant_service import GlobalAssistantService
from api.application.services.query_quota_gate import QueryQuotaGate
from api.application.services.zalo_bot_identity import global_session_id, opaque_identity
from api.application.services.zalo_bot_processor import ZaloBotProcessor, ZaloEvent

CHAT = "private-chat-not-for-logs"
TOKEN = "test-provider-token-not-for-logs"
SECRET = "test-only-independent-identity-secret"
ANSWER = "Giá Camellia: 42 tỷ."


class MemoryStore:
    def __init__(self):
        self.events = {1: ZaloEvent(1, CHAT, "Camellia giá bao nhiêu?")}
        self.binding_exists = True
        self.binding_active = True
        self.sales_active = True
        self.authorization_error = None
        self.authorization_calls = []
        self.fail_once = None
        self.after_cache = None
        self.writes = []

    def _write(self, operation):
        if self.fail_once == operation:
            self.fail_once = None
            raise RuntimeError(f"{CHAT} {TOKEN}")
        self.writes.append(operation)

    async def get_event(self, event_id):
        # Detached DB-row snapshot, not mutable references shared with processor.
        return deepcopy(self.events.get(event_id))

    async def authorize(self, identity):
        self.authorization_calls.append(identity)
        if self.authorization_error:
            raise self.authorization_error
        if self.binding_exists and self.binding_active and self.sales_active:
            return 73
        return None

    async def set_identity(self, event_id, identity, source):
        self._write("identity")
        self.events[event_id].identity_key = identity
        self.events[event_id].source_event_key = source

    async def cache_outcome(self, event_id, outcome):
        self._write("cache")
        self.events[event_id].assistant_outcome = deepcopy(outcome)
        if self.after_cache:
            self.after_cache()

    async def record_outbound(self, event_id, message_id):
        self._write("outbound")
        self.events[event_id].outbound_message_id = message_id

    async def set_status(self, event_id, status):
        self._write(status)
        self.events[event_id].status = status


class MemoryHistory:
    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.reads = []
        self.persisted = []
        self.after_persist = None
        self.fail_persist = False

    async def global_session(self, *, session_id, identity_key, device_id=None):
        key = (identity_key, session_id)
        self.reads.append(("session", key))
        return self.sessions.get(key)

    async def global_messages(self, *, session_id, identity_key, device_id=None):
        key = (identity_key, session_id)
        self.reads.append(("messages", key))
        return deepcopy(self.messages.get(key, []))

    async def persist_global_turn(self, **turn):
        if self.fail_persist:
            return False
        key = (turn["identity_key"], turn["session_id"])
        self.persisted.append(deepcopy(turn))
        project = turn["resolved_project_key"]
        session = self.sessions.setdefault(key, SimpleNamespace(active_project_key=None))
        if project is not None:
            session.active_project_key = project
        self.messages.setdefault(key, []).extend([
            SimpleNamespace(role="user", content=turn["user_content"], project_key=project, meta={}),
            SimpleNamespace(role="assistant", content=turn["assistant_content"],
                            project_key=project, meta=deepcopy(turn["assistant_meta"])),
        ])
        if self.after_persist:
            self.after_persist()
        return True


class DeduplicatingAdmission:
    """Recording test stub only; not a production technical brake."""

    def __init__(self):
        self.calls = []
        self.admitted = set()
        self.allowed = True

    async def __call__(self, identity, source):
        self.calls.append((identity, source))
        if (identity, source) in self.admitted:
            return True
        if not self.allowed:
            return False
        self.admitted.add((identity, source))
        return True


@pytest.fixture(autouse=True)
def forbidden_business_effects(monkeypatch):
    """Patch real public entry points, not disconnected counters."""
    spies = []
    targets = [
        (QueryQuotaGate, "prepare_turn", AsyncMock),
        (anon_identity, "mint_anonymous_identity_token", Mock),
        (lead_service, "create_customer_lead", AsyncMock),
        (lead_service, "grant_post_lead_bonus_turns", AsyncMock),
    ]
    targets.extend((quota_service, name, AsyncMock) for name in (
        "consume_turn", "finalize_turn", "refund_turn", "grant_bonus",
        "link_identity", "grant_turns", "get_snapshot",
    ))
    for owner, name, mock_type in targets:
        spy = mock_type(side_effect=AssertionError(f"Forbidden Zalo operation: {name}"))
        monkeypatch.setattr(owner, name, spy)
        spies.append(spy)
    yield spies
    for spy in spies:
        spy.assert_not_called()


@pytest.fixture
def rig(monkeypatch):
    store = MemoryStore()
    history = MemoryHistory()
    pipeline = SimpleNamespace(run=AsyncMock(return_value={
        "answer": ANSWER,
        "sources": [{"internal": TOKEN}],
        "images": [{"url": "https://invalid.example/private-image"}],
        "routing": {"lead": "must-not-be-forwarded"},
    }))
    factory = Mock(return_value=pipeline)
    catalogue = AsyncMock(return_value=[("camellia", "Camellia"), ("soleil", "Soleil")])
    core = GlobalAssistantService(history=history, pipeline_factory=factory, catalogue_loader=catalogue)
    monkeypatch.setattr(core, "run", AsyncMock(wraps=core.run))
    monkeypatch.setattr(core, "prepare_turn", AsyncMock(wraps=core.prepare_turn))
    sender = SimpleNamespace(
        send_text=AsyncMock(return_value="provider-1"),
        send_media=AsyncMock(side_effect=AssertionError("No media outbound")),
    )
    admission = DeduplicatingAdmission()
    processor = ZaloBotProcessor(store=store, core=core, sender=sender,
                                 identity_secret=SECRET, admit=admission)
    result = SimpleNamespace(store=store, history=history, pipeline=pipeline,
                             factory=factory, catalogue=catalogue, core=core,
                             sender=sender, admission=admission, processor=processor)
    yield result
    sender.send_media.assert_not_called()


def assert_no_core_work(rig):
    rig.core.run.assert_not_called()
    rig.core.prepare_turn.assert_not_called()
    rig.factory.assert_not_called()
    rig.pipeline.run.assert_not_called()
    rig.catalogue.assert_not_called()
    assert rig.history.reads == []
    assert rig.history.messages == {}
    assert rig.history.persisted == []
    rig.sender.send_text.assert_not_called()
    assert rig.admission.calls == []


@pytest.mark.parametrize("denial", ["unknown", "revoked", "inactive"])
def test_actor_denial_before_any_core_or_transcript(rig, denial):
    if denial == "unknown":
        rig.store.binding_exists = False
    elif denial == "revoked":
        rig.store.binding_active = False
    else:
        rig.store.sales_active = False
    # Self-asserted sales information in provider text cannot authorize actor.
    rig.store.events[1].text = "sales_id=73 ownership_verified=true Camellia"
    assert asyncio.run(rig.processor.process(1)) == "rejected"
    assert rig.store.events[1].status == "rejected"
    assert_no_core_work(rig)


class UndefinedTable(RuntimeError):
    pass


@pytest.mark.parametrize("error", [
    TimeoutError("binding timeout"),
    ConnectionError("binding unavailable"),
    UndefinedTable("missing binding schema"),
])
def test_authorization_outage_stays_retryable_without_core(rig, error):
    rig.store.authorization_error = error
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.events[1].status == "pending"
    assert rig.store.writes == []
    assert_no_core_work(rig)


def test_missing_event_does_not_authorize(rig):
    assert asyncio.run(rig.processor.process(999)) == "missing"
    assert rig.store.authorization_calls == []
    assert_no_core_work(rig)


def test_default_admission_blocks_uncached_live_work(rig):
    processor = ZaloBotProcessor(store=rig.store, core=rig.core, sender=rig.sender,
                                 identity_secret=SECRET)
    assert asyncio.run(processor.process(1)) == "blocked"
    assert rig.store.events[1].status == "pending"
    assert len(rig.store.authorization_calls) == 1
    assert_no_core_work(rig)


def test_injected_admission_denial_never_enters_core(rig):
    rig.admission.allowed = False
    assert asyncio.run(rig.processor.process(1)) == "blocked"
    assert rig.store.events[1].status == "pending"
    rig.core.run.assert_not_called()
    rig.pipeline.run.assert_not_called()
    assert rig.history.reads == []


@pytest.mark.parametrize("query,status,project,rag_calls", [
    ("Camellia giá bao nhiêu?", "resolved", "camellia", 1),
    ("Dự án nào có hồ bơi?", "unresolved", None, 0),
    ("Camellia Soleil", "ambiguous", None, 0),
])
def test_authorized_shared_core_outcomes(rig, query, status, project, rag_calls):
    rig.store.events[1].text = query
    assert asyncio.run(rig.processor.process(1)) == "done"
    event = rig.store.events[1]
    outcome = event.assistant_outcome
    assert outcome["resolution_status"] == status
    assert outcome["project_key"] == project
    assert outcome["answer"] == rig.history.persisted[0]["assistant_content"]
    assert set(outcome) == {"answer", "resolution_status", "project_key", "meta"}
    assert outcome["meta"] == {}
    rig.sender.send_text.assert_awaited_once_with(CHAT, outcome["answer"])
    assert rig.pipeline.run.await_count == rag_calls
    assert len(rig.history.persisted) == 1
    assert event.outbound_message_id == "provider-1"
    assert rig.store.writes.index("cache") < rig.store.writes.index("outbound") < rig.store.writes.index("done")
    request = rig.core.run.call_args.args[0]
    identity = opaque_identity(CHAT, SECRET)
    assert request.query == query
    assert request.identity_key == event.identity_key == identity
    assert request.session_id == global_session_id(identity)
    assert CHAT not in request.session_id
    assert CHAT not in request.identity_key
    assert request.source_event_key == event.source_event_key == "zalo-bot:event:v1:1"
    assert request.device_id is request.route_project_key is request.selected_project_key is request.on_event is None
    assert rig.store.authorization_calls == [identity, identity]


def test_followup_uses_core_persisted_active_project_and_history(rig):
    assert asyncio.run(rig.processor.process(1)) == "done"
    rig.store.events[2] = ZaloEvent(2, CHAT, "Còn tiện ích thì sao?")
    assert asyncio.run(rig.processor.process(2)) == "done"
    assert rig.store.events[2].assistant_outcome["project_key"] == "camellia"
    followup = rig.pipeline.run.call_args
    assert followup.kwargs["project_key"] == "camellia"
    assert followup.args[3] == [
        {"role": "user", "content": "Camellia giá bao nhiêu?"},
        {"role": "assistant", "content": ANSWER},
    ]
    assert len(rig.history.persisted) == 2
    assert len(rig.history.messages) == 1
    assert [row.project_key for row in next(iter(rig.history.messages.values()))] == ["camellia"] * 4


def test_crash_before_cache_recovers_real_core_without_duplicate_rag_or_transcript(rig):
    rig.store.fail_once = "cache"
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.events[1].assistant_outcome is None
    assert rig.store.events[1].status == "pending"
    rig.sender.send_text.assert_not_called()
    assert len(rig.history.persisted) == 1
    # Prior source already admitted; replay allowed even after stub closes new admissions.
    rig.admission.allowed = False
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert rig.core.run.await_count == 2
    assert rig.pipeline.run.await_count == 1
    assert len(rig.history.persisted) == 1
    assert len(next(iter(rig.history.messages.values()))) == 2
    assert len(rig.admission.admitted) == 1
    assert rig.admission.calls[0] == rig.admission.calls[1]
    rig.sender.send_text.assert_awaited_once_with(CHAT, ANSWER)


def test_cached_send_retry_skips_core_and_admission_even_with_default_guard(rig):
    rig.sender.send_text.side_effect = RuntimeError("send unavailable")
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.events[1].assistant_outcome["answer"] == ANSWER
    assert rig.store.events[1].status == "pending"
    rig.sender.send_text.side_effect = None
    processor = ZaloBotProcessor(store=rig.store, core=rig.core, sender=rig.sender,
                                 identity_secret=SECRET)
    assert asyncio.run(processor.process(1)) == "done"
    assert rig.core.run.await_count == 1
    assert len(rig.admission.calls) == 1
    assert len(rig.history.persisted) == 1
    assert rig.sender.send_text.await_count == 2


def test_provider_id_persisted_before_done_skips_send_on_retry(rig):
    rig.store.fail_once = "done"
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.events[1].outbound_message_id == "provider-1"
    assert rig.store.events[1].status == "pending"
    before_authorize = len(rig.store.authorization_calls)
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert len(rig.store.authorization_calls) == before_authorize + 1
    assert rig.core.run.await_count == 1
    assert rig.sender.send_text.await_count == 1
    assert len(rig.admission.calls) == 1


@pytest.mark.parametrize("phase", ["cached", "persisted", "provider_id"])
def test_revoked_retry_cannot_read_history_replay_cache_or_send(rig, phase):
    if phase == "cached":
        rig.sender.send_text.side_effect = RuntimeError("send unavailable")
    elif phase == "persisted":
        rig.store.fail_once = "cache"
    else:
        rig.store.fail_once = "done"
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    before = (rig.core.run.await_count, len(rig.history.reads),
              rig.sender.send_text.await_count, len(rig.admission.calls))
    rig.store.binding_active = False
    assert asyncio.run(rig.processor.process(1)) == "rejected"
    assert (rig.core.run.await_count, len(rig.history.reads),
            rig.sender.send_text.await_count, len(rig.admission.calls)) == before


@pytest.mark.parametrize("phase", ["core", "cache"])
def test_revocation_during_processing_blocks_outbound(rig, phase):
    def revoke():
        rig.store.binding_active = False

    if phase == "core":
        rig.history.after_persist = revoke
    else:
        rig.store.after_cache = revoke
    assert asyncio.run(rig.processor.process(1)) == "rejected"
    assert len(rig.history.persisted) == 1
    assert rig.store.events[1].assistant_outcome is not None
    rig.sender.send_text.assert_not_called()


@pytest.mark.parametrize("failure", ["pipeline", "persistence"])
def test_core_failure_leaves_event_pending_without_send(rig, failure):
    if failure == "pipeline":
        rig.pipeline.run.side_effect = RuntimeError(f"{CHAT} {TOKEN}")
    else:
        rig.history.fail_persist = True
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.events[1].status == "pending"
    assert rig.store.events[1].assistant_outcome is None
    assert rig.history.persisted == []
    rig.sender.send_text.assert_not_called()


@pytest.mark.parametrize("size", [2000, 2001, 6000])
def test_answer_bound_once_before_cache_and_identical_on_retry(rig, size):
    answer = "ấ" * size
    rig.pipeline.run.return_value = {"answer": answer}
    rig.sender.send_text.side_effect = RuntimeError("send unavailable")
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    suffix = "\n[Đã rút gọn]"
    expected = answer if size <= 2000 else answer[:2000 - len(suffix)] + suffix
    assert rig.store.events[1].assistant_outcome["answer"] == expected
    rig.sender.send_text.side_effect = None
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert [call.args[1] for call in rig.sender.send_text.call_args_list] == [expected, expected]
    assert len(expected) == 2000
    assert rig.history.persisted[0]["assistant_content"] == answer


@pytest.mark.parametrize("failure", ["authorize", "core", "store", "sender"])
def test_error_logs_never_include_chat_token_or_exception_text(rig, caplog, failure):
    error = RuntimeError(f"https://provider.invalid/bot/{TOKEN} chat={CHAT}")
    if failure == "authorize":
        rig.store.authorization_error = error
    elif failure == "core":
        rig.pipeline.run.side_effect = error
    elif failure == "store":
        rig.store.fail_once = "cache"
    else:
        rig.sender.send_text.side_effect = error
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert caplog.records
    assert TOKEN not in caplog.text
    assert CHAT not in caplog.text
    assert "provider.invalid" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("cached", [False, True])
def test_authorization_outage_before_replay_or_cache_remains_pending(rig, cached):
    if cached:
        rig.sender.send_text.side_effect = RuntimeError("send unavailable")
    else:
        rig.store.fail_once = "cache"
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    before = (rig.core.run.await_count, len(rig.history.reads),
              rig.sender.send_text.await_count, len(rig.admission.calls))
    rig.store.authorization_error = TimeoutError("authorization unavailable")
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.events[1].status == "pending"
    assert (rig.core.run.await_count, len(rig.history.reads),
            rig.sender.send_text.await_count, len(rig.admission.calls)) == before


def test_authorization_outage_after_core_preserves_cache_without_send(rig):
    def lose_authorization_store():
        rig.store.authorization_error = ConnectionError("authorization unavailable")

    rig.store.after_cache = lose_authorization_store
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.events[1].status == "pending"
    assert rig.store.events[1].assistant_outcome["answer"] == ANSWER
    rig.sender.send_text.assert_not_called()


def test_persisted_source_does_not_bypass_default_live_guard(rig):
    rig.store.fail_once = "cache"
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    processor = ZaloBotProcessor(store=rig.store, core=rig.core, sender=rig.sender,
                                 identity_secret=SECRET)
    assert asyncio.run(processor.process(1)) == "blocked"
    assert rig.core.run.await_count == 1
    rig.sender.send_text.assert_not_called()


def test_ingress_persists_and_acks_without_waiting_for_authorization(rig, monkeypatch):
    import json

    from pydantic import SecretStr
    from starlette.requests import Request

    from api.interfaces.api import zalo_bot_routes

    async def scenario():
        order = []
        rig.store.events.clear()
        monkeypatch.setattr(zalo_bot_routes, "_background_tasks", set())
        monkeypatch.setattr(zalo_bot_routes, "get_settings", lambda: SimpleNamespace(
            zalo_bot_enabled=True,
            zalo_bot_webhook_secret=SecretStr("test-webhook-secret"),
            zalo_bot_max_body_bytes=65536,
        ))

        async def persist(**values):
            assert rig.store.authorization_calls == []
            order.append("persist")
            rig.store.events[1] = ZaloEvent(1, values["chat_id"], values["text"])
            return 1

        async def process(event_id):
            assert event_id in rig.store.events
            order.append("process")
            assert await rig.processor.process(event_id) == "done"

        monkeypatch.setattr(zalo_bot_routes, "schedule", process)
        payload = {"ok": True, "result": {
            "event_name": "message.text.received",
            "message": {"message_id": "ingress-1", "text": "Camellia?",
                        "chat": {"id": CHAT, "chat_type": "PRIVATE"}},
        }}

        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(),
                    "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/webhook", "headers": [
            (b"content-type", b"application/json"),
            (b"x-bot-api-secret-token", b"test-webhook-secret"),
        ]}
        response = await zalo_bot_routes.zalo_webhook(Request(scope, receive), event_store=persist)
        assert response.status_code == 200
        assert json.loads(response.body) == {"ok": True, "accepted": True}
        assert rig.store.authorization_calls == []

        async def send(message):
            if message["type"] == "http.response.body":
                order.append("ack")

        await response(scope, receive, send)
        await asyncio.gather(*tuple(zalo_bot_routes._background_tasks))
        assert order == ["persist", "ack", "process"]
        assert rig.store.events[1].status == "done"

    asyncio.run(scenario())
