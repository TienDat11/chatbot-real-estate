"""Messenger ingress projection contracts. DB-free; pure projection only."""

from __future__ import annotations

import logging

from api.application.services.messenger_bot_projection import (
    InboundReason,
    project_messenger_event,
)
from api.domain.value_objects.constants import MAX_QUERY_LENGTH

_PSID_MARKER = "psid-marker-8f3a2c1d9e"
_TEXT_MARKER = "text-marker-must-not-be-logged-7b4e2a10"


def _msg(psid="PSID123", mid="m_mid.1", text="hello", **extra):
    return {
        "object": "page",
        "entry": [
            {
                "id": "PAGE1",
                "time": 1,
                "messaging": [
                    {"sender": {"id": psid}, "message": {"mid": mid, "text": text, **extra}},
                ],
            }
        ],
    }


def test_page_text_accepted_message():
    results = project_messenger_event(_msg(text="Can ho 2PN"))
    assert len(results) == 1
    r = results[0]
    assert r.reason == InboundReason.ACCEPTED
    assert r.event is not None
    assert r.event.kind == "message"
    assert r.event.psid == "PSID123"
    assert r.event.event_ref == "m_mid.1"
    assert r.event.text == "Can ho 2PN"
    assert r.event.selected_project_key is None


def test_select_project_quick_reply():
    payload = _msg(mid="m_mid.2", text="pick", quick_reply={"payload": "SELECT_PROJECT:soleil"})
    (r,) = project_messenger_event(payload)
    assert r.reason == InboundReason.ACCEPTED
    assert r.event is not None
    assert r.event.kind == "select_project"
    assert r.event.selected_project_key == "soleil"
    assert r.event.event_ref == "m_mid.2"


def test_get_started_postback():
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "PAGE1",
                "time": 1,
                "messaging": [
                    {
                        "sender": {"id": "PSID9"},
                        "postback": {"payload": "GET_STARTED", "mid": "m_pb.1"},
                    }
                ],
            }
        ],
    }
    (r,) = project_messenger_event(payload)
    assert r.reason == InboundReason.ACCEPTED
    assert r.event is not None
    assert r.event.kind == "get_started"
    assert r.event.event_ref == "m_pb.1"


def test_echo_is_echo():
    (r,) = project_messenger_event(_msg(is_echo=True))
    assert r.reason == InboundReason.ECHO
    assert r.event is None


def test_select_project_menu_quick_reply_is_get_started():
    payload = _msg(mid="m_mid.3", text="menu", quick_reply={"payload": "SELECT_PROJECT_MENU"})
    (r,) = project_messenger_event(payload)
    assert r.reason == InboundReason.ACCEPTED
    assert r.event is not None
    assert r.event.kind == "get_started"


def test_batch_two_messages_in_order():
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "PAGE1",
                "time": 1,
                "messaging": [
                    {"sender": {"id": "PSID-A"}, "message": {"mid": "m_mid.a", "text": "first"}},
                    {"sender": {"id": "PSID-B"}, "message": {"mid": "m_mid.b", "text": "second"}},
                ],
            }
        ],
    }
    results = project_messenger_event(payload)
    assert len(results) == 2
    assert results[0].reason == InboundReason.ACCEPTED
    assert results[1].reason == InboundReason.ACCEPTED
    assert results[0].event is not None and results[1].event is not None
    assert (results[0].event.event_ref, results[0].event.text) == ("m_mid.a", "first")
    assert (results[1].event.event_ref, results[1].event.text) == ("m_mid.b", "second")


def test_non_page_object_rejected():
    (r,) = project_messenger_event({"object": "user", "entry": []})
    assert r.reason == InboundReason.NOT_PAGE_OBJECT
    assert r.event is None


def test_text_too_long():
    (r,) = project_messenger_event(_msg(text="x" * (MAX_QUERY_LENGTH + 1)))
    assert r.reason == InboundReason.TEXT_TOO_LONG
    assert r.event is None


def test_wrong_page_entry_rejected():
    """An entry for a Page we are not subscribed to is dropped, not processed.

    Without this check a shared app secret would let any Page's webhook deliver
    events into this tenant's queue.
    """
    payload = _msg(text="Can ho 2PN")
    payload["entry"][0]["id"] = "PAGE2"
    (r,) = project_messenger_event(payload, page_id="PAGE1")
    assert r.reason == InboundReason.WRONG_PAGE
    assert r.event is None


def test_matching_page_entry_accepted():
    (r,) = project_messenger_event(_msg(text="Can ho 2PN"), page_id="PAGE1")
    assert r.reason == InboundReason.ACCEPTED
    assert r.event is not None


def test_page_check_skipped_when_unconfigured():
    """An unconfigured Page id cannot be distinguished from a mismatch."""
    payload = _msg(text="Can ho 2PN")
    payload["entry"][0]["id"] = "PAGE2"
    (r,) = project_messenger_event(payload)
    assert r.reason == InboundReason.ACCEPTED
    (r_empty,) = project_messenger_event(payload, page_id="")
    assert r_empty.reason == InboundReason.ACCEPTED


def test_wrong_page_entry_dropped_while_sibling_page_lands():
    """Per-entry check: one wrong entry does not reject the whole batch."""
    payload = {
        "object": "page",
        "entry": [
            {"id": "PAGE2", "time": 1,
             "messaging": [_msg(mid="m_mid.bad")["entry"][0]["messaging"][0]]},
            {"id": "PAGE1", "time": 1,
             "messaging": [_msg(mid="m_mid.good")["entry"][0]["messaging"][0]]},
        ],
    }
    results = project_messenger_event(payload, page_id="PAGE1")
    assert [r.reason for r in results] == [
        InboundReason.WRONG_PAGE,
        InboundReason.ACCEPTED,
    ]
    assert results[1].event is not None
    assert results[1].event.event_ref == "m_mid.good"


def test_recipient_id_mismatch_is_wrong_page():
    """recipient.id names the Page that received the event; a mismatch is rejected."""
    payload = _msg(text="Can ho 2PN")
    payload["entry"][0]["messaging"][0]["recipient"] = {"id": "PAGE2"}
    (r,) = project_messenger_event(payload, page_id="PAGE1")
    assert r.reason == InboundReason.WRONG_PAGE


def test_absent_recipient_id_is_not_a_drop():
    """Meta omits recipient on some event types; absence must not reject."""
    (r,) = project_messenger_event(_msg(text="Can ho 2PN"), page_id="PAGE1")
    assert r.reason == InboundReason.ACCEPTED


def test_matching_recipient_id_accepted():
    payload = _msg(text="Can ho 2PN")
    payload["entry"][0]["messaging"][0]["recipient"] = {"id": "PAGE1"}
    (r,) = project_messenger_event(payload, page_id="PAGE1")
    assert r.reason == InboundReason.ACCEPTED


def test_echo_on_wrong_recipient_reports_echo_first():
    """Fixed order echo -> page(recipient): an echo is dropped before the Page
    check, so a Page never sees its own outbound traffic reported as a tenant
    mismatch. entry.id is a batch-level prefilter and is kept matching here."""
    payload = _msg(text="Can ho 2PN", is_echo=True)
    payload["entry"][0]["messaging"][0]["recipient"] = {"id": "PAGE2"}
    (r,) = project_messenger_event(payload, page_id="PAGE1")
    assert r.reason == InboundReason.ECHO


def test_wrong_entry_id_wins_over_echo():
    """entry.id is a batch prefilter: a foreign entry is dropped whole, so its
    messaging is never inspected at all."""
    payload = _msg(text="Can ho 2PN", is_echo=True)
    payload["entry"][0]["id"] = "PAGE2"
    (r,) = project_messenger_event(payload, page_id="PAGE1")
    assert r.reason == InboundReason.WRONG_PAGE


def test_page_id_not_a_string_is_wrong_page():
    payload = _msg(text="Can ho 2PN")
    payload["entry"][0]["id"] = 12345
    (r,) = project_messenger_event(payload, page_id="PAGE1")
    assert r.reason == InboundReason.WRONG_PAGE


def test_no_leak_psid_or_text_in_logs(caplog):
    with caplog.at_level(logging.INFO):
        results = project_messenger_event(_msg(psid=_PSID_MARKER, mid="m_mid.x", text=_TEXT_MARKER))
    assert len(results) == 1
    assert results[0].reason == InboundReason.ACCEPTED
    assert _PSID_MARKER not in caplog.text
    assert _TEXT_MARKER not in caplog.text
