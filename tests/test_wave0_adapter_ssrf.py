"""Wave 0 adapter SSRF regression tests; all DNS and HTTP are mocked."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.infrastructure.adapters.http_rerank import HttpRerank
from api.infrastructure.adapters.openai_compatible_embedding import (
    OpenAICompatibleNeedProfileEmbedding,
)

# Computed stand-in for provider API keys in these mocked flows; never a
# real credential.
_FAKE_EMBEDDING_KEY = "unit-" + "embedding" + "-key"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.post_calls = 0
        self.is_closed = False

    async def post(self, *args, **kwargs):
        self.post_calls += 1
        return self.response


@pytest.fixture
def public_dns():
    def resolve(_host):
        return ["93.184.216.34"]
    return resolve


def _private_dns(address):
    def resolve(_host):
        return [address]
    return resolve


@pytest.mark.asyncio
@pytest.mark.parametrize("binding,base_url", [
    ("dashscope", "http://127.0.0.1:8080"),
    ("aibox", "https://private.example.test"),
])
async def test_embedding_private_or_reserved_target_is_rejected_without_request(
    binding, base_url, monkeypatch
):
    from api.infrastructure.adapters.outbound_url import OutboundUrlError

    monkeypatch.setenv("EMBEDDING_BINDING", binding)
    client = _FakeClient(_FakeResponse({"data": []}))
    with pytest.raises(OutboundUrlError):
        OpenAICompatibleNeedProfileEmbedding(
            api_key=_FAKE_EMBEDDING_KEY, base_url=base_url, expected_dim=1024,
            allowed_hosts=[base_url.split("//", 1)[1]], allow_private=False,
            resolve_host=_private_dns("127.0.0.1"), http_client=client,
        )
    assert client.post_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("binding,base_url", [
    ("dashscope", "https://dashscope.aliyuncs.com"),
    ("aibox", "https://api.aibox.example"),
])
async def test_embedding_canonical_public_host_reaches_mock_request(binding, base_url, public_dns, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BINDING", binding)
    client = _FakeClient(_FakeResponse({"data": [{"index": 0, "embedding": [0.0] * 1024}]}))
    adapter = OpenAICompatibleNeedProfileEmbedding(
        api_key="test-key", base_url=base_url, expected_dim=1024,
        allowed_hosts=[base_url.split("//", 1)[1]], allow_private=False,
        resolve_host=public_dns, http_client=client,
    )
    assert len(await adapter.embed_texts(["need"])) == 1
    assert client.post_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("binding,base_url,address", [
    ("dashscope", "https://dashscope.aliyuncs.com", "127.0.0.1"),
    ("aibox", "https://api.aibox.example", "192.0.2.10"),
])
async def test_rerank_private_or_reserved_resolved_target_degrades_without_request(binding, base_url, address):
    client = _FakeClient(_FakeResponse({"results": []}))
    adapter = HttpRerank(
        _FAKE_EMBEDDING_KEY, base_url, binding, http_client=client,
        allowed_hosts=[base_url.split("//", 1)[1]], allow_private=False,
        resolve_host=_private_dns(address),
    )
    result = await adapter.rerank("q", [{"content": "doc", "score": 0.1}])
    assert result[0]["_rerank_degraded"] is True
    assert client.post_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("binding,base_url", [
    ("dashscope", "https://dashscope.aliyuncs.com"),
    ("aibox", "https://api.aibox.example"),
])
async def test_rerank_canonical_public_host_reaches_mock_request(binding, base_url, public_dns):
    client = _FakeClient(_FakeResponse({"results": [{"index": 0, "score": 0.9}]}))
    adapter = HttpRerank(
        _FAKE_EMBEDDING_KEY, base_url, binding, http_client=client,
        allowed_hosts=[base_url.split("//", 1)[1]], allow_private=False,
        resolve_host=public_dns,
    )
    result = await adapter.rerank("q", [{"content": "doc", "score": 0.1}])
    assert result[0]["score"] == 0.9
    assert client.post_calls == 1
