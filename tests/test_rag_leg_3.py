import pytest

from api.application.services import rag_leg
from tests.fixtures.rag_leg_common import FakeRag


@pytest.mark.asyncio
async def test_missing_ids_are_explicitly_degraded(monkeypatch):
    fake = FakeRag([{"data": {"chunks": [{"content": "orphan"}]}}])
    monkeypatch.setattr(rag_leg, "_get_rag", lambda project_key=None: fake)
    monkeypatch.setattr(rag_leg, "_make_query_param", lambda *_: object())
    async def _no_kept_chunks(*_args):
        return []

    monkeypatch.setattr(rag_leg, "_post_filter", _no_kept_chunks)

    result = await rag_leg.run_rag_leg("q", [], [], None)

    assert result.degraded is True
    assert result.degraded_reasons == ("chunks_missing_ids",)
