"""DB-free contract tests for the Messenger cache/replay validators."""

from __future__ import annotations

import pytest

from api.infrastructure.adapters.postgres_messenger_events import (
    MessengerStorageError,
    decode_messenger_cache,
    valid_messenger_cache,
)


def _base(**overrides):
    payload = {
        "reply_kind": "answer",
        "text": "Can ho 2PN gia 3 ty",
        "options": None,
        "picker_keys": None,
        "candidate_order": None,
        "resolution_status": "resolved",
        "project_key": None,
    }
    payload.update(overrides)
    return payload


def test_decode_answer_valid():
    assert decode_messenger_cache(_base())["reply_kind"] == "answer"


def test_decode_quick_replies_valid():
    payload = _base(reply_kind="quick_replies", options=[["Xem tiep", "MORE"], ["Lien he", "CONTACT"]])
    assert decode_messenger_cache(payload)["options"][0] == ["Xem tiep", "MORE"]


def test_decode_numbered_picker_valid():
    payload = _base(reply_kind="numbered_picker", picker_keys=["alpha", "beta"],
                    candidate_order=["alpha", "beta", "gamma"])
    assert decode_messenger_cache(payload)["picker_keys"] == ["alpha", "beta"]


def test_decode_brake_valid():
    payload = _base(reply_kind="brake", text="He thong qua tai, thu lai sau",
                    resolution_status="technical_brake")
    assert valid_messenger_cache(payload) is True


def test_decode_missing_key_rejected():
    payload = _base()
    del payload["picker_keys"]
    with pytest.raises(MessengerStorageError):
        decode_messenger_cache(payload)
    assert valid_messenger_cache(payload) is False


def test_decode_empty_text_rejected():
    assert valid_messenger_cache(_base(text="   ")) is False


def test_decode_overlong_text_rejected():
    assert valid_messenger_cache(_base(text="x" * 2001)) is False


def test_decode_fourteen_options_rejected():
    options = [[f"Opt{i}", f"P{i}"] for i in range(14)]
    assert valid_messenger_cache(_base(reply_kind="quick_replies", options=options)) is False


def test_decode_unknown_status_rejected():
    assert valid_messenger_cache(_base(resolution_status="mystery")) is False
