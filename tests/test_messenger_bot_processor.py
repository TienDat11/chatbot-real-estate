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
    decode_messenger_cache,
    valid_messenger_cache,
)
from api.application.ports.messenger_bot import MessengerSendError
from api.application.services.messenger_bot_rendering import to_messenger_plain
from api.infrastructure.adapters.postgres_messenger_events import MessengerStorageError


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

    async def send_image(self, psid, url):
        """Record an image attachment send and return its provider message id."""
        self.calls.append(("send_image", psid, url))
        return f"mid.img.{len(self.calls)}"



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


def test_to_messenger_plain_table_three_columns_preserves_cells():
    """3-column markdown table: separator row dropped, no pipes left, all cell text kept."""
    table = (
        "| dự án | giá | diện tích |\n"
        "|---|---|---|\n"
        "| Camellia | 3 tỷ | 100 m² |\n"
        "| Soleil | 2 tỷ | 80 m² |"
    )
    out = to_messenger_plain(table)
    assert "|" not in out
    assert "dự án — giá — diện tích" in out
    assert "Camellia — 3 tỷ — 100 m²" in out
    assert "Soleil — 2 tỷ — 80 m²" in out


def test_to_messenger_plain_strips_bold_heading_bullets():
    """Bold, underlined bold, ATX headings and bullets are normalized."""
    md = (
        "**bold text** and __also bold__\n"
        "# Heading One\n"
        "## Subheading\n"
        "- bullet one\n"
        "- bullet two\n"
        "* bullet star\n"
        "+ bullet plus\n"
        "1. first ordered\n"
        "2. second ordered"
    )
    out = to_messenger_plain(md)
    assert out == (
        "bold text and also bold\n"
        "Heading One\n"
        "Subheading\n"
        "• bullet one\n"
        "• bullet two\n"
        "• bullet star\n"
        "• bullet plus\n"
        "1. first ordered\n"
        "2. second ordered"
    )


def test_to_messenger_plain_plain_vietnamese_unchanged():
    """Plain Vietnamese prose without markdown is returned byte-identical."""
    viet = "Xin chào! Dự án Camellia có giá từ 3 tỷ. Liên hệ ngay hôm nay để nhận ưu đãi."
    assert to_messenger_plain(viet) == viet


def test_delivery_sends_image_attachments_after_text(rig):
    """Delivery sends the rendered text first, then each image, ids in order."""
    cached = {
        "reply_kind": "answer",
        "text": "**Bold answer**",
        "options": None,
        "picker_keys": None,
        "candidate_order": None,
        "resolution_status": "resolved",
        "project_key": None,
        "images": [
            "https://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev/img1.png",
            "https://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev/img2.png",
        ],
    }
    _reset_event(rig)
    rig.store.events[1].assistant_outcome = cached
    assert valid_messenger_cache(cached)
    assert _run(rig) == "done"
    assert rig.sender.calls == [
        ("send_text", CHAT, "Bold answer"),
        ("send_image", CHAT, "https://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev/img1.png"),
        ("send_image", CHAT, "https://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev/img2.png"),
    ]


def test_delivery_swallows_image_send_error_and_completes(rig):
    """A MessengerSendError on image send is swallowed; text delivery already succeeded."""
    cached = {
        "reply_kind": "answer",
        "text": "Answer",
        "options": None,
        "picker_keys": None,
        "candidate_order": None,
        "resolution_status": "resolved",
        "project_key": None,
        "images": ["https://a.com/img.png"],
    }
    _reset_event(rig)
    rig.store.events[1].assistant_outcome = cached

    async def failing_send_image(psid, url):
        raise MessengerSendError(code="INVALID_MEDIA", retryable=False)

    rig.sender.send_image = failing_send_image
    assert _run(rig) == "done"
    assert rig.sender.calls == [("send_text", CHAT, "Answer")]
    assert rig.store.events[1].status == "done"
    assert rig.store.events[1].outbound_message_ids == ["mid.text.1"]


def test_decode_messenger_cache_accepts_legacy_and_new_image_key():
    """decode_messenger_cache accepts the legacy 7-key shape and the new 8-key shape."""
    legacy = {
        "reply_kind": "answer",
        "text": "plain",
        "options": None,
        "picker_keys": None,
        "candidate_order": None,
        "resolution_status": "resolved",
        "project_key": None,
    }
    new = dict(legacy, images=["https://a.com/1.png"])
    assert decode_messenger_cache(legacy) is not None
    assert "images" not in decode_messenger_cache(legacy)
    decoded = decode_messenger_cache(new)
    assert decoded["images"] == ["https://a.com/1.png"]

    too_many = dict(legacy, images=["https://a.com/1.png"] * 4)
    with pytest.raises(MessengerStorageError, match="INVALID_CACHE"):
        decode_messenger_cache(too_many)

    empty_url = dict(legacy, images=["", "https://b.com/2.png"])
    with pytest.raises(MessengerStorageError, match="INVALID_CACHE"):
        decode_messenger_cache(empty_url)

    non_string = dict(legacy, images=[123])
    with pytest.raises(MessengerStorageError, match="INVALID_CACHE"):
        decode_messenger_cache(non_string)

    too_long = dict(legacy, images=["x" * 2001])
    with pytest.raises(MessengerStorageError, match="INVALID_CACHE"):
        decode_messenger_cache(too_long)


def test_delivery_falls_back_to_raw_text_when_plain_render_is_empty(rig, monkeypatch):
    """If plain rendering yields empty, the raw text is sent instead of nothing."""
    import api.application.services.messenger_bot_processor as processor_mod

    cached = {
        "reply_kind": "answer",
        "text": "DROPPED",
        "options": None,
        "picker_keys": None,
        "candidate_order": None,
        "resolution_status": "resolved",
        "project_key": None,
        "images": [],
    }
    _reset_event(rig)
    rig.store.events[1].assistant_outcome = cached

    orig = processor_mod.to_messenger_plain
    monkeypatch.setattr(processor_mod, "to_messenger_plain", lambda t: "" if t == "DROPPED" else orig(t))

    assert _run(rig) == "done"
    assert rig.sender.calls == [("send_text", CHAT, "DROPPED")]


def _media_rig(rig, urls):
    """Rebuild the processor with a media loader returning `urls` per project."""
    loader = AsyncMock(return_value=urls)
    rig.processor = MessengerBotProcessor(
        store=rig.store, core=rig.core, sender=rig.sender,
        identity_secret=SECRET, admit=rig.admission, settings=rig.settings,
        catalogue_loader=rig.catalogue, media_loader=loader)
    return loader


def test_picker_greeting_has_no_images_and_no_send_image(rig):
    """A new-conversation picker must not attach any project's media."""
    loader = _media_rig(
        rig, ["https://media.dev/floorplan.png", "https://media.dev/amenity.png"])
    _reset_event(rig, kind="get_started", text="")
    assert _run(rig) == "done"
    assert "images" not in rig.store.events[1].assistant_outcome
    assert loader.await_count == 0
    assert all(c[0] != "send_image" for c in rig.sender.calls)


def test_refreshed_picker_after_stale_selection_has_no_images(rig):
    """A stale select_project key refreshes the picker without media attached."""
    loader = _media_rig(rig, ["https://media.dev/floorplan.png"])
    _reset_event(rig, kind="select_project", selected_project_key="ghost")
    assert _run(rig) == "done"
    outcome = rig.store.events[1].assistant_outcome
    assert outcome["reply_kind"] == "quick_replies"
    assert "images" not in outcome
    assert loader.await_count == 0
    assert all(c[0] != "send_image" for c in rig.sender.calls)


def test_intro_resolved_project_caps_images_at_two(rig):
    """Messenger caps an intro's media at two urls even when the loader returns more."""
    loader = _media_rig(rig, [
        "https://media.dev/1.png", "https://media.dev/2.png",
        "https://media.dev/3.png", "https://media.dev/4.png", "https://media.dev/5.png"])
    _reset_event(rig, kind="select_project", selected_project_key="camellia")
    assert _run(rig) == "done"
    outcome = rig.store.events[1].assistant_outcome
    assert outcome["project_key"] == "camellia"
    assert outcome["images"] == [
        "https://media.dev/1.png", "https://media.dev/2.png"]
    assert [c for c in rig.sender.calls if c[0] == "send_image"] == [
        ("send_image", CHAT, "https://media.dev/1.png"),
        ("send_image", CHAT, "https://media.dev/2.png")]


def test_intro_single_image_is_sent_without_padding(rig):
    """A single loader url yields exactly one image; the cap never pads the set."""
    _media_rig(rig, ["https://media.dev/only.png"])
    _reset_event(rig, kind="select_project", selected_project_key="camellia")
    assert _run(rig) == "done"
    assert rig.store.events[1].assistant_outcome["images"] == ["https://media.dev/only.png"]
    assert [c for c in rig.sender.calls if c[0] == "send_image"] == [
        ("send_image", CHAT, "https://media.dev/only.png")]


def test_intro_without_images_sends_no_image(rig):
    """An empty loader result leaves the reply without images and sends none."""
    _media_rig(rig, [])
    _reset_event(rig, kind="select_project", selected_project_key="camellia")
    assert _run(rig) == "done"
    assert "images" not in rig.store.events[1].assistant_outcome
    assert all(c[0] != "send_image" for c in rig.sender.calls)


def test_delivery_caps_images_at_two_for_legacy_cached_envelope(rig):
    """A cached envelope holding three urls (pre-cap rows) still sends only two images."""
    cached = {
        "reply_kind": "answer",
        "text": "Answer",
        "options": None,
        "picker_keys": None,
        "candidate_order": None,
        "resolution_status": "resolved",
        "project_key": None,
        "images": [
            "https://media.dev/1.png", "https://media.dev/2.png", "https://media.dev/3.png"],
    }
    _reset_event(rig)
    rig.store.events[1].assistant_outcome = cached
    assert _run(rig) == "done"
    assert [c for c in rig.sender.calls if c[0] == "send_image"] == [
        ("send_image", CHAT, "https://media.dev/1.png"),
        ("send_image", CHAT, "https://media.dev/2.png")]

