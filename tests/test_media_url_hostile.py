"""Hostile URL contract tests for the future server-side media fetcher."""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from api.application.services.media_http import (
    perform_media_head,
    validate_media_fetch_url,
)

PUBLIC_ORIGIN = "https://cdn.example.test"


async def public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "ftp://cdn.example.test/file.jpg",
        "javascript://cdn.example.test/file.jpg",
        "data:text/plain,hello",
        "file:///etc/passwd",
        "https://localhost/file.jpg",
        "https://127.0.0.1/file.jpg",
        "https://10.1.2.3/file.jpg",
        "https://172.20.1.2/file.jpg",
        "https://192.168.1.2/file.jpg",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/file.jpg",
        "https://[fc00::1]/file.jpg",
        "https://[fe80::1]/file.jpg",
        "https://224.0.0.1/file.jpg",
        "https://[ff02::1]/file.jpg",
        "https://0.0.0.0/file.jpg",
        "https://100.64.0.1/file.jpg",
        "https://198.18.0.1/file.jpg",
        "https://240.0.0.1/file.jpg",
        "https://metadata.google.internal/file.jpg",
        "https://metadata.goog/file.jpg",
        "https://instance-data/file.jpg",
        "https://100.100.100.200/latest/meta-data/",
        "https://user:secret@cdn.example.test/file.jpg",
        "https://cdn.example.test@evil.example/file.jpg",
        "https://evil-cdn.example.test/file.jpg",
        "https://cdn.example.test.evil.example/file.jpg",
        "https://[::ffff:127.0.0.1]/file.jpg",
        "https://[2002:7f00:1::1]/file.jpg",
        "https://xn--localhost-9za.example/file.jpg",
    ],
)
async def test_hostile_urls_are_rejected(url: str) -> None:
    assert await validate_media_fetch_url(url, [PUBLIC_ORIGIN], resolve=public_resolver) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["10.0.0.1", "192.168.1.1", "127.0.0.1"])
async def test_any_private_dns_record_rejects_host(address: str) -> None:
    async def rebinding_resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34", address]

    assert (
        await validate_media_fetch_url(
            f"{PUBLIC_ORIGIN}/file.jpg", [PUBLIC_ORIGIN], resolve=rebinding_resolver
        )
        is None
    )


@pytest.mark.asyncio
async def test_dns_failure_and_non_ip_records_fail_closed() -> None:
    async def failing_resolver(host: str, port: int) -> list[str]:
        raise OSError("resolver unavailable")

    async def malformed_resolver(host: str, port: int) -> list[str]:
        return ["not-an-ip"]

    assert await validate_media_fetch_url(
        f"{PUBLIC_ORIGIN}/file.jpg", [PUBLIC_ORIGIN], resolve=failing_resolver
    ) is None
    assert await validate_media_fetch_url(
        f"{PUBLIC_ORIGIN}/file.jpg", [PUBLIC_ORIGIN], resolve=malformed_resolver
    ) is None


@pytest.mark.asyncio
async def test_allowlist_is_exact_and_success_normalizes_idn_origin() -> None:
    assert await validate_media_fetch_url(
        "https://cdn.example.test/file.jpg", [PUBLIC_ORIGIN], resolve=public_resolver
    ) == "https://cdn.example.test/file.jpg"
    assert await validate_media_fetch_url(
        "https://CDN.EXAMPLE.TEST/file.jpg", [PUBLIC_ORIGIN], resolve=public_resolver
    ) == "https://CDN.EXAMPLE.TEST/file.jpg"


@pytest.mark.asyncio
async def test_redirect_to_private_target_is_rejected_before_second_request() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private.jpg"},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    result = await perform_media_head(
        f"{PUBLIC_ORIGIN}/redirect.jpg",
        [PUBLIC_ORIGIN],
        transport=transport,
        resolve=public_resolver,
        max_redirect_hops=1,
    )
    assert result is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_content_type_and_size_caps_are_enforced() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": "999"},
            request=request,
        )

    result = await perform_media_head(
        f"{PUBLIC_ORIGIN}/file.jpg",
        [PUBLIC_ORIGIN],
        transport=httpx.MockTransport(handler),
        resolve=public_resolver,
        max_response_bytes=10,
    )
    assert result is None


@pytest.mark.parametrize("network", ["192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"])
def test_denied_network_table_is_ipaddress_valid(network: str) -> None:
    assert ipaddress.ip_network(network).num_addresses > 0


# --- Wired-caller contract (ISSUE-3): the CDN migration probe is a real
# production caller of this module and must inherit every fail-closed rule.

@pytest.mark.asyncio
async def test_wired_migration_probe_uses_project_allowlist_and_no_credentials(
    monkeypatch,
) -> None:
    from api.infrastructure.config.config import settings as app_settings
    from scripts.migrate_cdn_origins import probe_media_url

    seen_headers: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(
            {key.lower(): value for key, value in request.headers.items()}
        )
        return httpx.Response(
            200, headers={"content-type": "image/png"}, request=request
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        app_settings,
        "image_cdn_project_map",
        '{"camellia":"https://cdn.example.test"}',
    )

    verdict = await probe_media_url(
        f"{PUBLIC_ORIGIN}/images/camellia/matbang/a.png",
        "camellia",
        transport=transport,
        resolve=public_resolver,
    )
    assert verdict is not None and verdict.status_code == 200
    # No credential headers are ever attached to an outbound media probe.
    assert "authorization" not in seen_headers
    assert seen_headers.get("user-agent")

    # An origin outside the project map is rejected without any network touch.
    assert (
        await probe_media_url(
            "https://evil.example.test/images/camellia/a.png",
            "camellia",
            transport=transport,
            resolve=public_resolver,
        )
        is None
    )


@pytest.mark.asyncio
async def test_wired_migration_probe_fails_closed_without_allowlist() -> None:
    from api.infrastructure.config.config import settings as app_settings
    from scripts.migrate_cdn_origins import probe_media_url

    monkey_unmapped = '{"soleil":"https://soleil-origin.example.test"}'
    original = app_settings.image_cdn_project_map
    app_settings.image_cdn_project_map = monkey_unmapped
    try:
        assert (
            await probe_media_url(
                f"{PUBLIC_ORIGIN}/images/camellia/a.png", "camellia"
            )
            is None
        )
    finally:
        app_settings.image_cdn_project_map = original
