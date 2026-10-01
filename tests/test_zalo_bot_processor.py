"""ZB-02 transport boundary with real shared-core replay and project history.

Only injected memory stores/pipelines are used; no provider, DB, or factory choice.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from api.application.ports.zalo_bot import ZaloAdmissionDecision

import pytest

from api.application.services import anon_identity, lead_service, quota_service
from api.application.services.global_assistant_service import GlobalAssistantService, _bound_visible
from api.application.services.query_quota_gate import QueryQuotaGate
from api.application.services.zalo_bot_identity import global_session_id, opaque_identity
from api.application.services.zalo_bot_processor import ZaloBotProcessor, ZaloEvent

CHAT = "private-chat-not-for-logs"
TOKEN = "test-provider-token-not-for-logs"
SECRET = "test-only-independent-identity-secret"
ANSWER = "Giá Camellia: 42 tỷ."


class MemoryStore:
    def __init__(self):
        self.events = {1: self.event(1, "Camellia giá bao nhiêu?")}
        self.binding_active = True
        self.authorization_error = None
        self.authorization_calls = []
        self.fail_once = None
        self.after_cache = None
        self.writes = []
        self.codes = []
        self.remaining = 120.0

    @staticmethod
    def event(event_id, text):
        event = ZaloEvent(event_id, CHAT, text, identity_key=opaque_identity(CHAT, SECRET),
                          source_event_key=f"zalo-bot:event:v1:{event_id}", ingress_key="ingress")
        event.completed_at = None
        return event

    def _write(self, operation):
        if self.fail_once == operation:
            self.fail_once = None
            raise RuntimeError(f"{CHAT} {TOKEN}")
        self.writes.append(operation)

    async def claim_event(self, event_id):
        event = self.events.get(event_id)
        if event is None or event.status in {"done", "dead", "rejected", "processing"}:
            return None
        event.claim_token = uuid4()
        event.status = "processing"
        event.attempt_count += 1
        event.lease_remaining = self.remaining
        return deepcopy(event)

    async def authorize(self, identity):
        self.authorization_calls.append(identity)
        if self.authorization_error:
            raise self.authorization_error
        return 73 if self.binding_active else None

    def owns(self, event):
        live = self.events[event.id]
        return live.status == "processing" and live.claim_token == event.claim_token and self.remaining > 0

    async def check_ownership(self, event, *, minimum_remaining=0):
        return self.remaining if self.owns(event) and self.remaining > minimum_remaining else None

    async def renew_lease(self, event):
        return await self.check_ownership(event)

    async def cache_outcome(self, event, outcome):
        if not self.owns(event):
            return False
        self._write("cache")
        self.events[event.id].assistant_outcome = deepcopy(outcome)
        if self.after_cache:
            self.after_cache()
        return True

    async def complete(self, event, message_id=None):
        if not self.owns(event):
            return False
        self._write("done")
        live = self.events[event.id]
        live.outbound_message_id = message_id or live.outbound_message_id
        live.status, live.claim_token = "done", None
        live.completed_at = datetime.now(timezone.utc)
        return True

    async def fail(self, event, code, *, retryable=True):
        if not self.owns(event):
            return False
        self.codes.append(code)
        live = self.events[event.id]
        live.status = "failed" if retryable and live.attempt_count < 3 else "dead"
        live.claim_token = None
        return True

    async def reject(self, event):
        if not self.owns(event):
            return False
        self.events[event.id].status = "rejected"
        self.events[event.id].claim_token = None
        self.events[event.id].completed_at = datetime.now(timezone.utc)
        return True

    async def release(self, event):
        return await self.fail(event, "RELEASED")


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
    def __init__(self):
        self.calls = []
        self.admitted = {}
        self.allowed = True

    async def __call__(self, identity, source, *, event_id, claim_token):
        self.calls.append((identity, source))
        key = (identity, source)
        if key in self.admitted:
            return (ZaloAdmissionDecision.ALREADY_STARTED if self.admitted[key]
                    else ZaloAdmissionDecision.ALREADY_BRAKED)
        self.admitted[key] = self.allowed
        return ZaloAdmissionDecision.STARTED if self.allowed else ZaloAdmissionDecision.BRAKED


@pytest.fixture(autouse=True)
def forbidden_business_effects(monkeypatch):
    """Patch real public entry points, not disconnected counters."""
    spies = []
    targets = [
        (QueryQuotaGate, "prepare_turn", AsyncMock),
        (QueryQuotaGate, "finalize_turn", AsyncMock),
        (QueryQuotaGate, "refund_turn", AsyncMock),
        (anon_identity, "mint_anonymous_identity_token", Mock),
        (lead_service, "create_customer_lead", AsyncMock),
        (lead_service, "grant_post_lead_bonus_turns", AsyncMock),
    ]
    targets.extend((quota_service, name, AsyncMock) for name in (
        "consume_turn", "finalize_turn", "refund_turn", "grant_bonus",
        "link_identity", "grant_turns", "get_snapshot",
    ))
    import sys
    from api.application.services import query_quota_gate

    targets.extend((query_quota_gate, name, AsyncMock) for name in (
        "consume_turn", "finalize_reserved_turn", "refund_reserved_turn",
    ))
    targets.append((lead_service, "grant_bonus", AsyncMock))
    for module_name, names in (
        ("api.interfaces.api.lead", ("create_customer_lead", "grant_post_lead_bonus_turns")),
        ("api.interfaces.api.auth_routes", ("mint_anonymous_identity_token",)),
    ):
        module = sys.modules.get(module_name)
        if module is not None:
            targets.extend((module, name, AsyncMock) for name in names if hasattr(module, name))
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
    monkeypatch.setattr(core, "recover_source_event", AsyncMock(wraps=core.recover_source_event))
    monkeypatch.setattr(core, "prepare_turn", AsyncMock(wraps=core.prepare_turn))
    sender = SimpleNamespace(
        send_text=AsyncMock(return_value="provider-1"),
        send_media=AsyncMock(side_effect=AssertionError("No media outbound")),
    )
    admission = DeduplicatingAdmission()
    settings = SimpleNamespace(zalo_bot_enabled=True, zalo_bot_core_timeout_seconds=90,
                               zalo_bot_lease_seconds=120, zalo_bot_lease_margin_seconds=15,
                               zalo_bot_open_demo_access=False)
    processor = ZaloBotProcessor(store=store, core=core, sender=sender,
                                 identity_secret=SECRET, admit=admission, settings=settings)
    result = SimpleNamespace(store=store, history=history, pipeline=pipeline,
                             factory=factory, catalogue=catalogue, core=core, settings=settings,
                             sender=sender, admission=admission, processor=processor)
    yield result
    sender.send_media.assert_not_called()


def assert_no_core_work(rig):
    rig.core.run.assert_not_called()
    rig.core.recover_source_event.assert_not_called()
    rig.core.prepare_turn.assert_not_called()
    rig.factory.assert_not_called()
    rig.pipeline.run.assert_not_called()
    rig.catalogue.assert_not_called()
    assert rig.history.reads == []
    assert rig.history.messages == {}
    assert rig.history.persisted == []
    rig.sender.send_text.assert_not_called()
    assert rig.admission.calls == []


def test_denial_blocks_all_work(rig):
    rig.store.binding_active = False
    assert asyncio.run(rig.processor.process(1)) == "rejected"
    assert rig.store.events[1].status == "rejected"
    assert_no_core_work(rig)


def test_authorization_outage_retries_closed(rig):
    rig.store.authorization_error = TimeoutError("private")
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.codes == ["AUTH_UNAVAILABLE"]
    assert_no_core_work(rig)


def test_disabled_and_missing_admission_are_closed(rig):
    rig.settings.zalo_bot_enabled = False
    assert asyncio.run(rig.processor.process(1)) == "skipped"
    assert rig.store.events[1].attempt_count == 0
    rig.settings.zalo_bot_enabled = True
    rig.processor._admit = None
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.codes == ["ADMISSION_UNAVAILABLE"]
    assert_no_core_work(rig)


@pytest.mark.parametrize("query,status,project,rag_calls", [
    ("Camellia giá bao nhiêu?", "resolved", "camellia", 1),
    ("Dự án nào có hồ bơi?", "unresolved", None, 0),
    ("Camellia Soleil", "ambiguous", None, 0),
])
def test_authorized_shared_core_outcomes(rig, query, status, project, rag_calls):
    rig.store.events[1].text = query
    assert asyncio.run(rig.processor.process(1)) == "done"
    event = rig.store.events[1]
    assert event.assistant_outcome["resolution_status"] == status
    assert event.assistant_outcome["project_key"] == project
    assert event.assistant_outcome["answer"] == rig.history.persisted[0]["assistant_content"]
    rig.sender.send_text.assert_awaited_once_with(CHAT, event.assistant_outcome["answer"])
    assert rig.pipeline.run.await_count == rag_calls
    assert len(rig.history.persisted) == 1
    assert event.outbound_message_id == "provider-1"


def test_followup_uses_shared_history(rig):
    assert asyncio.run(rig.processor.process(1)) == "done"
    rig.store.events[2] = rig.store.event(2, "Còn tiện ích thì sao?")
    assert asyncio.run(rig.processor.process(2)) == "done"
    assert rig.store.events[2].assistant_outcome["project_key"] == "camellia"
    assert rig.pipeline.run.call_args.args[3] == [
        {"role": "user", "content": "Camellia giá bao nhiêu?"},
        {"role": "assistant", "content": ANSWER},
    ]
    assert len(rig.history.persisted) == 2
    assert len(rig.history.messages) == 1


def test_before_cache_crash_uses_replay_only(rig):
    rig.store.fail_once = "cache"
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    rig.admission.allowed = False
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert rig.core.run.await_count == 1
    assert rig.core.prepare_turn.await_count == 1
    assert rig.core.recover_source_event.await_count == 1
    assert rig.pipeline.run.await_count == 1
    assert rig.catalogue.await_count == 1
    assert len(rig.history.persisted) == 1


@pytest.mark.parametrize("query,status,project,rag_calls", [
    ("Camellia giá bao nhiêu?", "resolved", "camellia", 1),
    ("Dự án nào có hồ bơi?", "unresolved", None, 0),
    ("Camellia Soleil", "ambiguous", None, 0),
])
def test_replay_outcome_bytes_and_scope_match_first_outcome(
        rig, monkeypatch, query, status, project, rag_calls):
    """Level-B: the replay served on the retry is the SAME outcome as the first pass.

    Pass 1 runs the core and persists history, then dies before the event row is
    cached. Pass 2 therefore has no cached outcome and no provider receipt, so the
    processor MUST go through `recover_source_event`. This asserts the replay is
    byte-identical and scope-identical to the first outcome, is reconstructed from
    the persisted envelope, and never re-runs resolution, pipeline, or persistence.
    """
    ran, replayed = [], []

    async def run_spy(request):
        outcome = await GlobalAssistantService.run(rig.core, request)
        ran.append(outcome)
        return outcome

    async def recover_spy(request):
        outcome = await GlobalAssistantService.recover_source_event(rig.core, request)
        replayed.append(outcome)
        return outcome

    monkeypatch.setattr(rig.core, "run", AsyncMock(side_effect=run_spy))
    monkeypatch.setattr(rig.core, "recover_source_event", AsyncMock(side_effect=recover_spy))
    rig.store.events[1].text = query

    rig.store.fail_once = "cache"
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert len(ran) == 1 and replayed == []
    # Nothing reached the provider and no outcome reached the event row.
    assert rig.store.events[1].assistant_outcome is None
    assert rig.store.events[1].outbound_message_id is None
    rig.sender.send_text.assert_not_called()

    first = ran[0]
    first_bytes = first.answer.encode("utf-8")
    assert first.resolution_status == status
    assert first.project_key == project
    assert first.replayed is False
    # What pass 1 emitted is exactly what it persisted.
    assert rig.history.persisted[0]["assistant_content"] == first.answer
    envelope = rig.history.persisted[0]["assistant_meta"]["ga_outcome"]
    assert envelope["answer"].encode("utf-8") == first_bytes
    assert envelope["resolution_status"] == status
    assert envelope["project_key"] == project

    assert asyncio.run(rig.processor.process(1)) == "done"
    assert len(ran) == 1, "core re-ran on the replayed pass"
    assert len(replayed) == 1
    replay = replayed[0]
    replay_bytes = replay.answer.encode("utf-8")

    assert replay.replayed is True
    assert replay.rag_ran is False
    assert first_bytes == replay_bytes
    assert first.resolution_status == replay.resolution_status == status
    assert first.project_key == replay.project_key == project
    assert first.resolution_source == replay.resolution_source
    assert first.candidates == replay.candidates

    # Served from cache/history: no second resolution, pipeline, or persistence.
    assert rig.core.run.await_count == 1
    assert rig.core.recover_source_event.await_count == 1
    assert rig.core.prepare_turn.await_count == 1
    assert rig.catalogue.await_count == 1
    assert rig.factory.call_count == rag_calls
    assert rig.pipeline.run.await_count == rag_calls
    assert len(rig.history.persisted) == 1
    assert rig.admission.calls == [
        (rig.store.events[1].identity_key, rig.store.events[1].source_event_key)] * 2

    # Envelope written on the replay pass is the first outcome, byte for byte.
    event = rig.store.events[1]
    assert event.assistant_outcome == {"answer": first.answer, "resolution_status": status,
                                       "project_key": project, "meta": {}}
    assert event.assistant_outcome["answer"].encode("utf-8") == replay_bytes
    assert event.status == "done"
    assert event.outbound_message_id == "provider-1"

    # The single outbound send carried the replayed bytes, not a re-generated answer.
    rig.sender.send_text.assert_awaited_once_with(CHAT, first.answer)
    assert rig.sender.send_text.call_args.args[1].encode("utf-8") == replay_bytes


def test_lost_receipt_ack_never_runs_on_retry(rig):
    original = rig.admission

    async def uncertain(*args, **kwargs):
        await original(*args, **kwargs)
        raise TimeoutError("commit acknowledgement lost")

    rig.processor._admit = uncertain
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    rig.processor._admit = original
    for _ in range(2):
        assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.events[1].status == "dead"
    assert rig.store.codes[-1] == "CORE_OUTCOME_UNCERTAIN"
    rig.core.run.assert_not_called()
    rig.core.prepare_turn.assert_not_called()
    rig.pipeline.run.assert_not_called()
    rig.catalogue.assert_not_called()
    assert rig.history.persisted == []
    rig.sender.send_text.assert_not_called()


def test_cached_retry_skips_admission_and_core(rig):
    rig.sender.send_text.side_effect = RuntimeError("private")
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    rig.processor._admit = None
    rig.sender.send_text.side_effect = None
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert rig.core.run.await_count == 1
    assert len(rig.admission.calls) == 1
    assert rig.sender.send_text.await_count == 2


def test_provider_acceptance_before_commit_can_duplicate_send_not_core(rig):
    rig.store.fail_once = "done"
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert rig.store.events[1].outbound_message_id is None
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert rig.sender.send_text.await_count == 2
    assert rig.core.run.await_count == 1


@pytest.mark.parametrize("authorization", ["active", "revoked", "outage"])
@pytest.mark.parametrize("cached", [None, {"answer": ANSWER, "resolution_status": "resolved", "project_key": None, "meta": {}}, "null"])
@pytest.mark.parametrize("provider_id", ["persisted", "", 0])
def test_provider_id_retry_does_not_resend_or_reclassify(rig, authorization, cached, provider_id):
    event = rig.store.events[1]
    event.status = "failed"
    event.outbound_message_id = provider_id
    event.assistant_outcome = cached
    rig.store.binding_active = authorization != "revoked"
    rig.store.authorization_error = TimeoutError("private") if authorization == "outage" else None
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert event.status == "done"
    assert event.completed_at is not None
    assert event.outbound_message_id == provider_id
    assert event.assistant_outcome == cached
    assert rig.store.authorization_calls == []
    assert rig.store.codes == []
    assert_no_core_work(rig)


def test_done_event_does_not_require_authorization(rig):
    event = rig.store.events[1]
    event.status = "done"
    event.completed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    before = deepcopy(vars(event))
    rig.store.authorization_error = TimeoutError("private")
    assert asyncio.run(rig.processor.process(1)) == "missing"
    assert vars(event) == before
    assert rig.store.authorization_calls == []
    assert_no_core_work(rig)


def test_rejected_event_does_not_require_authorization(rig):
    event = rig.store.events[1]
    event.status = "rejected"
    event.completed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    before = deepcopy(vars(event))
    rig.store.authorization_error = TimeoutError("private")
    assert asyncio.run(rig.processor.process(1)) == "missing"
    assert vars(event) == before
    assert rig.store.authorization_calls == []
    assert_no_core_work(rig)


def test_done_event_remains_done_after_sales_revoked(rig):
    event = rig.store.events[1]
    event.status = "done"
    event.completed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    before = deepcopy(vars(event))
    rig.store.binding_active = False
    assert asyncio.run(rig.processor.process(1)) == "missing"
    assert vars(event) == before
    assert rig.store.authorization_calls == []
    assert_no_core_work(rig)


@pytest.mark.parametrize("phase", ["cache", "history"])
def test_revoked_recovery_does_not_read_or_send(rig, phase):
    if phase == "cache":
        rig.store.events[1].assistant_outcome = {"answer": ANSWER, "resolution_status": "resolved", "project_key": "camellia", "meta": {}}
    else:
        rig.admission.admitted[(rig.store.events[1].identity_key, rig.store.events[1].source_event_key)] = True
    rig.store.binding_active = False
    assert asyncio.run(rig.processor.process(1)) == "rejected"
    assert_no_core_work(rig)


def test_revoked_after_core_blocks_outbound(rig):
    rig.store.after_cache = lambda: setattr(rig.store, "binding_active", False)
    assert asyncio.run(rig.processor.process(1)) == "rejected"
    assert len(rig.history.persisted) == 1
    rig.sender.send_text.assert_not_called()


def test_brake_is_transport_only_and_cached(rig):
    rig.admission.allowed = False
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert rig.store.events[1].assistant_outcome["resolution_status"] == "technical_brake"
    rig.core.run.assert_not_called()
    assert rig.history.reads == []
    assert rig.history.persisted == []


@pytest.mark.parametrize("size", [2000, 2001])
def test_bounded_answer_stable_on_retry(rig, size):
    """P0-05: the pipeline now applies the visible budget itself, so the mocked
    facade returns an ALREADY bounded answer. The processor's own `_bound_visible`
    is a no-op and the retry still sees the identical bytes."""
    answer = _bound_visible("ấ" * size, 2000)
    rig.pipeline.run.return_value = {"answer": answer}
    rig.sender.send_text.side_effect = RuntimeError("private")
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    rig.sender.send_text.side_effect = None
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert [call.args[1] for call in rig.sender.send_text.call_args_list] == [answer, answer]
    assert rig.history.persisted[0]["assistant_content"] == answer


def test_byte_identity_persisted_returned_sent_matches_bound(rig):
    """For an answer the pipeline truncated to the visible budget, persisted ==
    returned == sent are byte-identical, and that string is the bounded one."""
    answer = _bound_visible("ấ" * 2001, 2000)
    rig.pipeline.run.return_value = {"answer": answer}
    assert asyncio.run(rig.processor.process(1)) == "done"
    persisted = rig.history.persisted[0]["assistant_content"]
    sent = rig.sender.send_text.call_args.args[1]
    cached = rig.store.events[1].assistant_outcome["answer"]
    assert persisted == sent == cached == answer
    assert len(answer) <= 2000


@pytest.mark.parametrize("authorization", ["active", "revoked", "outage"])
@pytest.mark.parametrize("cached", [
    {}, [], "null",
    {"answer": "x", "resolution_status": "resolved", "project_key": None, "meta": {"secret": TOKEN}},
    *({"answer": answer, "resolution_status": "resolved", "project_key": None, "meta": {}}
      for answer in ("", " \n", "x" * 2001, 42, "\ud800")),
    *({"answer": "x", "resolution_status": status, "project_key": None, "meta": {}}
      for status in ("<script>", "unknown", " resolved ", " ", "", None, [])),
    {"answer": "x", "resolution_status": "resolved", "project_key": 42, "meta": {}},
    {"answer": "x", "resolution_status": "resolved", "project_key": None, "meta": {}, "source_event_key": "private"},
])
def test_invalid_cache_is_permanent_and_never_reruns(rig, cached, authorization):
    rig.store.events[1].assistant_outcome = cached
    rig.store.binding_active = authorization != "revoked"
    rig.store.authorization_error = TimeoutError("private") if authorization == "outage" else None
    assert asyncio.run(rig.processor.process(1)) == "dead"
    assert rig.store.events[1].status == "dead"
    assert rig.store.codes == ["CACHE_INVALID"]
    assert rig.store.authorization_calls == []
    assert asyncio.run(rig.processor.process(1)) == "missing"
    assert rig.store.codes == ["CACHE_INVALID"]
    assert_no_core_work(rig)


@pytest.mark.parametrize("authorization", ["active", "revoked", "outage"])
def test_json_null_claim_is_permanent_before_authorization(rig, monkeypatch, authorization):
    from api.infrastructure.adapters import postgres_zalo_events as events

    memory_claim = rig.store.claim_event

    async def claim(event_id):
        claimed = await memory_claim(event_id)
        row = vars(claimed) | {"assistant_outcome": "null"}
        conn = SimpleNamespace(
            fetchval=AsyncMock(side_effect=[claimed.identity_key, 120.0]),
            fetchrow=AsyncMock(side_effect=[
                {"holder_token": claimed.claim_token}, row,
                {"event_id": claimed.id, "source_event_key": claimed.source_event_key},
            ]),
        )

        @asynccontextmanager
        async def transaction():
            yield conn

        monkeypatch.setattr(events, "_transaction", transaction)
        monkeypatch.setattr(events, "uuid4", lambda: claimed.claim_token)
        settings = SimpleNamespace(zalo_bot_enabled=True, zalo_bot_lease_seconds=120,
                                   zalo_bot_max_attempts=3, zalo_bot_retry_max_age_hours=24)
        return await events.PostgresZaloEventStore(settings=settings).claim_event(event_id)

    monkeypatch.setattr(rig.store, "claim_event", claim)
    rig.store.binding_active = authorization != "revoked"
    rig.store.authorization_error = TimeoutError("private") if authorization == "outage" else None
    assert asyncio.run(rig.processor.process(1)) == "dead"
    assert rig.store.events[1].status == "dead"
    assert rig.store.codes == ["CACHE_INVALID"]
    assert rig.store.authorization_calls == []
    assert_no_core_work(rig)


@pytest.mark.parametrize("failure", ["authorize", "core", "cache", "sender"])
def test_private_failures_only_log_fixed_codes(rig, caplog, failure):
    error = RuntimeError(f"https://provider.invalid/bot/{TOKEN} chat={CHAT}")
    if failure == "authorize":
        rig.store.authorization_error = error
    elif failure == "core":
        rig.pipeline.run.side_effect = error
    elif failure == "cache":
        rig.store.fail_once = "cache"
    else:
        rig.sender.send_text.side_effect = error
    assert asyncio.run(rig.processor.process(1)) == "retryable"
    assert TOKEN not in caplog.text and CHAT not in caplog.text
    assert "provider.invalid" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_strict_mode_unknown_user_rejected_zero_core(rig):
    """Req 1: strict mode (binding revoked) rejects unknown user without core work."""
    rig.store.binding_active = False
    assert asyncio.run(rig.processor.process(1)) == "rejected"
    assert rig.store.authorization_calls == [rig.store.events[1].identity_key]
    assert_no_core_work(rig)


def test_open_demo_unknown_user_bypasses_authorize_core_and_sender(rig):
    """Req 2: open-demo mode permits unknown user; authorize() NOT called; core + sender execute."""
    rig.settings.zalo_bot_open_demo_access = True
    rig.store.binding_active = False
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert rig.store.authorization_calls == []
    assert rig.core.run.await_count == 1
    rig.sender.send_text.assert_awaited_once_with(CHAT, ANSWER)
    assert rig.history.persisted[0]["assistant_content"] == ANSWER


def test_open_demo_auth_store_outage_does_not_call_authorize(rig):
    """Req 3: open-demo outage — authorize() MUST NOT be called (no binding lookup)."""
    rig.settings.zalo_bot_open_demo_access = True
    rig.store.authorization_error = TimeoutError("private")
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert rig.store.authorization_calls == []
    assert rig.core.run.await_count == 1
    rig.sender.send_text.assert_awaited_once_with(CHAT, ANSWER)


def test_open_demo_cached_retry_skips_authorize_and_core(rig):
    """Req 4: open-demo cached outcome — authorize() NOT called, no core,
    sender sends cached answer."""
    rig.settings.zalo_bot_open_demo_access = True
    cached = {"answer": ANSWER, "resolution_status": "resolved", "project_key": None, "meta": {}}
    rig.store.events[1].assistant_outcome = cached
    rig.store.binding_active = False
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert rig.store.authorization_calls == []
    rig.core.run.assert_not_called()
    rig.sender.send_text.assert_awaited_once_with(CHAT, ANSWER)


def test_zalo_bot_disabled_overrides_open_demo(rig):
    """Req 5: ZALO_BOT_ENABLED=false overrides open-demo=True: skipped, zero work."""
    rig.settings.zalo_bot_open_demo_access = True
    rig.settings.zalo_bot_enabled = False
    assert asyncio.run(rig.processor.process(1)) == "skipped"
    assert rig.store.authorization_calls == []
    assert_no_core_work(rig)


def test_open_demo_technical_brake_still_works(rig):
    """Req 8: technical brake fires through open-demo — cached brake answer, no core."""
    rig.settings.zalo_bot_open_demo_access = True
    rig.admission.allowed = False
    assert asyncio.run(rig.processor.process(1)) == "done"
    assert rig.store.authorization_calls == []
    assert rig.history.persisted == []
    assert rig.store.events[1].assistant_outcome["resolution_status"] == "technical_brake"


