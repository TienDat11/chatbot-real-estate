"""Regression: firestore REST mirror client lifecycle is safe across event loops.

Pre-fix, ``firestore_rest_mirror`` cached ONE module-global ``httpx.AsyncClient``
created lazily on the first event loop it ran on. ``asyncio.run`` creates a fresh
loop per call, so the second loop reused a client bound to the first (already
closed) loop — requests broke and teardown raised ``RuntimeError: Event loop is
closed``, orphaning the mirror. The fix keys clients by the running loop (weak
keys + lock), so every loop owns its own client and a finished loop's client is
reaped with the loop.

This test drives the REAL ``get_client()`` cache through two separate
``asyncio.run`` loops with an ``httpx.MockTransport`` (no network), exercising
write + delete + explicit close in each loop, then asserts per-loop isolation
and an empty client cache (no orphan) after the deterministic close_client
sweep.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from api.infrastructure.adapters import firestore_rest_mirror
from api.infrastructure.adapters.firestore_rest_mirror import FirestoreRestLeadMirror
from api.infrastructure.ports.realtime_mirror import LeadMirrorDocument
from tests.fixtures.fake_credentials import fake_pem_private_key

PROJECT_ID = "sale-chat-bot-11e49"


def _lead_document() -> LeadMirrorDocument:
    return LeadMirrorDocument(
        customer_id="customer-digest-loop",
        project_key="camellia",
        lead_status="new",
        display_name=None,
        masked_phone=None,
        assigned_sales_firebase_uid=None,
        consent_service=False,
        consent_marketing=False,
        consent_recorded_at=None,
        last_customer_message_at=None,
        updated_at="2026-08-22T00:00:00+00:00",
        lead_id=11,
    )


def _make_mirror() -> FirestoreRestLeadMirror:
    return FirestoreRestLeadMirror(
        project_id=PROJECT_ID,
        service_account_client_email="mirror@developer.gserviceaccount.com",
        service_account_private_key=fake_pem_private_key(),
        rest_base_url="https://firestore.googleapis.com/v1",
    )


class _LoopRecorder:
    """Records every request the per-loop MockTransport handler serves."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url)))
        if request.method == "POST":
            # OAuth2 token exchange endpoint.
            return httpx.Response(200, json={"access_token": "tok-" + "loop" + "-test", "expires_in": 3600})
        return httpx.Response(200, json={})


def _run_loop_scenario(
    mirror: FirestoreRestLeadMirror, recorder: _LoopRecorder
) -> httpx.AsyncClient:
    """Run one full mirror lifecycle inside a single asyncio.run loop.

    Returns the client that the loop owned, proving per-loop isolation and that
    the loop-close did not break teardown.
    """

    async def scenario() -> httpx.AsyncClient:
        await mirror.upsert_lead_mirror(document_id="lead-doc-loop-1", document=_lead_document())
        await mirror.remove_lead_mirror("lead-doc-loop-1")
        # Explicit close while the loop is still live — the pre-fix code raised
        # "Event loop is closed" here when the client belonged to a dead loop.
        await firestore_rest_mirror.close_client()
        return await firestore_rest_mirror.get_client()

    return asyncio.run(scenario())


def test_mirror_client_is_loop_safe_across_asyncio_run_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _LoopRecorder()
    monkeypatch.setattr(
        firestore_rest_mirror,
        "_client_transport_factory",
        lambda: httpx.MockTransport(recorder.handle),
    )
    # Fresh token state so each loop must (re)exchange — exercising the POST path.
    monkeypatch.setattr(firestore_rest_mirror, "_cached_access_token", None)
    monkeypatch.setattr(firestore_rest_mirror, "_cached_access_token_expires_at", 0.0)
    mirror = _make_mirror()

    first_loop_client = _run_loop_scenario(mirror, recorder)
    second_loop_client = _run_loop_scenario(mirror, recorder)

    # Both loops served write (PATCH) + delete (DELETE) + token POST, in order.
    methods = [method for method, _ in recorder.requests]
    assert methods == ["POST", "PATCH", "DELETE", "POST", "PATCH", "DELETE"], methods

    # Per-loop isolation: the second asyncio.run loop must NOT reuse the first
    # loop's client (pre-fix the module-global client was handed across loops).
    assert first_loop_client is not second_loop_client
    assert not first_loop_client.is_closed
    assert not second_loop_client.is_closed

    # No orphan: reaping is deterministic, not GC-dependent. The client value
    # holds a strong reference back to its loop (httpx transport pool), so weak
    # keys alone can never collect finished-loop entries; close_client() sweeps
    # every entry whose loop reports is_closed() and drops it without awaiting a
    # dead-loop client.
    asyncio.run(firestore_rest_mirror.close_client())
    with firestore_rest_mirror._loop_clients_lock:
        cached = dict(firestore_rest_mirror._loop_clients)
    assert len(cached) == 0, f"orphaned mirror clients: {len(cached)}"


def test_mirror_close_client_from_no_running_loop_is_safe() -> None:
    """close_client() must not raise when called outside any event loop."""
    # Prime the cache inside a loop, then close from a fresh asyncio.run context
    # (a new loop) and from plain sync context.

    async def prime() -> httpx.AsyncClient:
        return await firestore_rest_mirror.get_client()

    client = asyncio.run(prime())
    assert client is not None

    # Sync context: no running loop — must not raise (pre-fix would try to
    # aclose a client bound to the now-closed loop and raise).
    asyncio.run(firestore_rest_mirror.close_client())

    # After closing from a different loop, a new call in a fresh loop must still
    # work (loop-safe recreation).
    new_client = asyncio.run(prime())
    assert new_client is not client
    assert not new_client.is_closed


def test_two_threads_two_loops_use_distinct_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent per-thread loops must each get their own client (no shared cache race)."""
    import threading

    recorder = _LoopRecorder()
    monkeypatch.setattr(
        firestore_rest_mirror,
        "_client_transport_factory",
        lambda: httpx.MockTransport(recorder.handle),
    )
    monkeypatch.setattr(firestore_rest_mirror, "_cached_access_token", None)
    monkeypatch.setattr(firestore_rest_mirror, "_cached_access_token_expires_at", 0.0)
    mirror = _make_mirror()

    results: dict[str, Any] = {}

    def worker(name: str) -> None:
        async def scenario() -> None:
            await mirror.upsert_lead_mirror(document_id="lead-thread", document=_lead_document())
            client = await firestore_rest_mirror.get_client()
            results[name] = (id(client), client.is_closed)

        asyncio.run(scenario())

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    ids = {client_id for client_id, _ in results.values()}
    assert len(ids) == 2, f"threads shared a client: {ids}"
    assert all(not closed for _, closed in results.values())

    # Both thread loops have finished: close_client()'s deterministic sweep
    # drops their closed-loop clients (weak keys alone cannot — the client holds
    # a strong reference back to its loop).
    asyncio.run(firestore_rest_mirror.close_client())
    with firestore_rest_mirror._loop_clients_lock:
        remaining = len(firestore_rest_mirror._loop_clients)
    assert remaining == 0, f"orphaned mirror clients after threads: {remaining}"
