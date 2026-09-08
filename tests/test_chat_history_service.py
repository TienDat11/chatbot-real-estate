import pytest

from api.application.services.chat_history_service import (
    ChatHistoryService,
    project_safe_meta,
)


class FakeHistory:
    def __init__(self):
        self.turns = []

    async def append_turn(self, **kwargs):
        self.turns.append(kwargs)

    async def list_sessions(self, **kwargs):
        return []

    async def list_messages(self, **kwargs):
        return []

    async def get_session_for_lead(self, **kwargs):
        return None

    async def mark_handed_off(self, **kwargs):
        pass

    async def cleanup(self, **kwargs):
        return 2

    async def get_cta_state(self, **kwargs):
        self.cta_kwargs = kwargs
        return (3, False, False)


@pytest.mark.asyncio
async def test_persist_turn_writes_user_and_assistant_metadata():
    repo = FakeHistory()
    service = ChatHistoryService(repo)
    await service.persist_turn(
        session_id="s1",
        device_id="d1",
        project_key="camellia",
        user_content="giá",
        assistant_content="42 tỷ",
        assistant_meta={"images": []},
    )
    assert repo.turns[0]["session_id"] == "s1"
    assert repo.turns[0]["assistant_meta"] == {"images": []}


@pytest.mark.asyncio
async def test_cta_state_is_durable_repository_value():
    repository = FakeHistory()
    service = ChatHistoryService(repository)
    assert await service.cta_state(
        session_id="s1", device_id="d1", project_key="camellia", identity_key="anon-1"
    ) == (3, False, False)
    assert repository.cta_kwargs == {
        "session_id": "s1",
        "device_id": "d1",
        "project_key": "camellia",
        "identity_key": "anon-1",
    }


def test_project_safe_meta_masks_nested_raw_phones_and_keeps_legitimate_metadata():
    """Shared transcript sanitizer (CRM + sales training surfaces): raw phones
    nested under facts/sources/images are masked recursively; non-sensitive
    values survive unchanged and unknown top-level keys stay dropped."""
    meta = {
        "facts": [{"fields": {"hotline": "0912 345 678", "area": 88}}],
        "sources": [{"doc_id": "d1", "title": "Chính sách bán hàng"}],
        "images": [{"caption": "Hotline (0912)-345-678"}],
        "internal": "drop-me",
    }
    safe = project_safe_meta(meta)
    assert safe["facts"][0]["fields"]["hotline"] == "0912***678"
    assert safe["facts"][0]["fields"]["area"] == 88
    assert safe["sources"][0]["doc_id"] == "d1"
    assert safe["sources"][0]["title"] == "Chính sách bán hàng"
    assert safe["images"][0]["caption"] == "Hotline 0912***678"
    assert "internal" not in safe


def test_project_safe_meta_masks_plus84_variant_and_leaves_non_phone_digits():
    assert project_safe_meta(None) is None
    assert project_safe_meta("not-a-dict") is None
    safe = project_safe_meta({"sources": [{"section": "+84 91 234 5678"}]})
    assert safe["sources"][0]["section"] == "+849***678"
    # Prices, dates, and short digit runs never get mangled by the redactor.
    untouched = {"facts": [{"fields": {"price": "1200000000", "date": "2026-08-31"}}]}
    assert project_safe_meta(untouched) == untouched
