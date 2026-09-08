import asyncio

import pytest

from api.application.services import rag_leg
from tests.fixtures.rag_leg_common import FakeRag


@pytest.mark.asyncio
async def test_aquery_timeout_retries_once_then_degrades(monkeypatch):
    fake = FakeRag([asyncio.TimeoutError(), asyncio.TimeoutError()])
    monkeypatch.setattr(rag_leg, "_get_rag", lambda project_key=None: fake)
    monkeypatch.setattr(rag_leg, "_make_query_param", lambda *_: object())

    result = await rag_leg.run_rag_leg("q", [], [], None)

    assert fake.calls == 2
    assert result.degraded is True
    assert result.degraded_reasons == ("aquery_timeout",)
