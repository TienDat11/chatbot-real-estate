"""Firestore REST mirror adapter — the no-SDK write path (stack lock bans firebase-admin).

The BE authenticates to Firestore REST v1 with an OAuth2 service-account JWT
grant: an RS256 assertion signed with PyJWT from the configured client email +
private key, exchanged at Google's token endpoint for a short-lived access
token scoped to datastore. The token is cached module-level until shortly
before expiry so steady-state writes cost one PATCH, not a token round-trip.

Authorization model (see docs/adr/0003-firestore-server-writer-authz.md):
server-authenticated REST/RPC access is governed by Google IAM, NOT by the
Firestore security rules — rules only evaluate mobile/web client requests.
PROVISIONING REQUIREMENT: the service account must be granted IAM
`roles/datastore.user` on the Firebase/GCP project; without it the mirror's
writes fail with 403 regardless of what firestore.rules says.

PG stays the source of truth (hybrid D1): this adapter only writes the
denormalized lead snapshot consumed by realtime clients, and never reads back.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import threading
import time
import weakref
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import jwt

from api.infrastructure.ports.realtime_mirror import (
    LeadMirrorDocument,
    RealtimeMirrorNotConfiguredError,
)

logger = logging.getLogger("api.adapters.firestore_rest_mirror")

# Refresh the OAuth2 access token this many seconds before its real expiry so a
# write never races the deadline.
_ACCESS_TOKEN_EARLY_REFRESH_SECONDS = 60.0
_OAUTH2_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_FIRESTORE_DATASTORE_SCOPE = "https://www.googleapis.com/auth/datastore"

# Loop-scoped AsyncClient cache. httpx.AsyncClient binds to the event loop that
# runs it; a single module-global client would be handed across the fresh loops
# that asyncio.run creates (scripts, tests, one-off jobs) and either schedule
# I/O on a dead loop or raise "Event loop is closed" at teardown, orphaning the
# mirror. Keying by the running loop gives every loop its own client; the weak
# keys help but cannot collect finished-loop entries on their own — the client
# value holds a strong reference back to its loop (httpx transport pool), so a
# dead loop is never GC'd and its weak key never fires. Reaping is therefore
# deterministic: every get_client/close_client sweeps entries whose loop reports
# is_closed() and drops them (no await on a dead-loop client). Locked because
# loop-scoped usage is not limited to one thread: two threads running their own
# loops may call get_client concurrently.
_loop_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)
_loop_clients_lock = threading.Lock()

# Test seam: transport constructor for the per-loop client. None selects the
# real network stack; tests swap in httpx.MockTransport to exercise the full
# per-loop lifecycle (creation, close, reaping) without live I/O.
_client_transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None

_RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

_cached_access_token: str | None = None
_cached_access_token_expires_at: float = 0.0


def _reap_closed_loop_clients() -> None:
    """Drop cached clients whose owning event loop is already closed.

    Weak cache keys alone cannot reclaim finished-loop entries: the client value
    holds a strong reference back to its loop (httpx transport pool), so the weak
    key never dies and the closed-loop client would be retained forever. Checking
    loop.is_closed() — a plain synchronous flag read, safe from any thread — makes
    reaping deterministic and order-independent. A closed loop's transports are
    already dead, so dropping the client (never awaiting aclose on a dead-loop
    client, which would raise "Event loop is closed") is the safe close.
    """
    with _loop_clients_lock:
        dead_loops = [loop for loop in _loop_clients if loop.is_closed()]
        for loop in dead_loops:
            _loop_clients.pop(loop, None)


async def get_client() -> httpx.AsyncClient:
    """Return the AsyncClient owned by the *current* event loop, creating on first use.

    Explicit bounded phase timeouts come from Settings. Loop-scoped so a client
    is never shared across asyncio.run loops — the pre-fix module-global cache
    bound to the first loop and broke every later loop's requests and teardown.
    """
    from api.infrastructure.config.config import get_settings

    s = get_settings()
    _reap_closed_loop_clients()
    loop = asyncio.get_running_loop()
    with _loop_clients_lock:
        client = _loop_clients.get(loop)
        if client is None or client.is_closed:
            timeout = httpx.Timeout(
                timeout=s.firestore_read_timeout_seconds,
                connect=s.firestore_connect_timeout_seconds,
            )
            transport = (
                _client_transport_factory() if _client_transport_factory is not None else None
            )
            client = httpx.AsyncClient(timeout=timeout, transport=transport)
            _loop_clients[loop] = client
    return client


async def _request_with_retries(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Retry only transport failures and 5xx responses for idempotent calls."""
    from api.infrastructure.config.config import get_settings

    attempts = max(1, min(get_settings().firestore_retry_attempts, 4))
    client = await get_client()
    for attempt in range(attempts):
        try:
            # Keep the transport seam compatible with lightweight test doubles that
            # expose verb methods only, while using httpx's generic request path in
            # production. Both paths receive the exact same retry policy.
            request = getattr(client, method.lower(), None)
            if request is None:
                response = await client.request(method, url, **kwargs)
            else:
                response = await request(url, **kwargs)
            if response.status_code < 500 or attempt == attempts - 1:
                return response
        except _RETRYABLE_EXCEPTIONS:
            if attempt == attempts - 1:
                raise
        await asyncio.sleep(min(0.5, 0.05 * (2**attempt)) + random.uniform(0, 0.05))
    raise RuntimeError("firestore request retry budget exhausted")


async def close_client() -> None:
    """Close this loop's AsyncClient and drop the cached OAuth2 token.

    Loop-safe teardown: only the caller's own loop is touched (awaiting a client
    whose loop is running elsewhere would corrupt that loop's I/O). Clients of
    finished loops are reaped deterministically by _reap_closed_loop_clients —
    dropping the reference to a dead-loop client is the safe equivalent of a
    close, since awaiting it would raise "Event loop is closed".
    """
    global _cached_access_token
    _cached_access_token = None
    _reap_closed_loop_clients()
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    client: httpx.AsyncClient | None = None
    if running_loop is not None:
        with _loop_clients_lock:
            client = _loop_clients.pop(running_loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()


def _document_fields(document: LeadMirrorDocument) -> dict[str, dict[str, Any]]:
    """Map the mirror dataclass into Firestore REST Value payloads, omitting Nones."""
    fields: dict[str, dict[str, Any]] = {
        "customer_id": {"stringValue": document.customer_id},
        # REST encodes int64 as a decimal string; the web SDK decodes safe
        # integers back to JS numbers, matching the FE mapper's expectation.
        "lead_id": {"integerValue": str(document.lead_id)},
        "project_key": {"stringValue": document.project_key},
        "lead_status": {"stringValue": document.lead_status},
        "consent_service": {"booleanValue": document.consent_service},
        "consent_marketing": {"booleanValue": document.consent_marketing},
        "updated_at": {"timestampValue": document.updated_at},
    }
    if document.created_at is not None:
        fields["created_at"] = {"timestampValue": document.created_at}
    optional_string_fields = {
        "display_name": document.display_name,
        "masked_phone": document.masked_phone,
        "assigned_sales_firebase_uid": document.assigned_sales_firebase_uid,
        "consent_recorded_at": document.consent_recorded_at,
        "last_customer_message_at": document.last_customer_message_at,
        "rejection_reason": document.rejection_reason,
        "reengage_at": document.reengage_at,
        "marketing_withdrawn_at": document.marketing_withdrawn_at,
    }
    for field_name, field_value in optional_string_fields.items():
        if field_value is not None:
            fields[field_name] = {"stringValue": field_value}
    return fields


class FirestoreRestLeadMirror:
    """Writes lead mirror documents to Firestore over REST v1 with an OAuth2 grant."""

    def __init__(
        self,
        *,
        project_id: str,
        service_account_client_email: str,
        service_account_private_key: str,
        rest_base_url: str,
    ) -> None:
        if not service_account_client_email or not service_account_private_key:
            # Fail fast at wiring time: a half-configured mirror would otherwise
            # surface as a confusing 401 on the first lead write.
            raise RealtimeMirrorNotConfiguredError(
                "firestore binding requires FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL "
                "and FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY"
            )
        self.project_id = project_id
        self.service_account_client_email = service_account_client_email
        # Env values carry the JSON key-file's \n escapes; normalize once here.
        self.service_account_private_key = service_account_private_key.replace("\\n", "\n")
        self.rest_base_url = rest_base_url.rstrip("/")

    def _document_url(self, document_id: str) -> str:
        return (
            f"{self.rest_base_url}/projects/{self.project_id}"
            f"/databases/(default)/documents/leads/{document_id}"
        )

    async def _fetch_access_token(self) -> str:
        """Exchange a signed service-account assertion for a scoped access token."""
        issued_at = int(time.time())
        assertion = jwt.encode(
            {
                "iss": self.service_account_client_email,
                "scope": _FIRESTORE_DATASTORE_SCOPE,
                "aud": _OAUTH2_TOKEN_ENDPOINT,
                "iat": issued_at,
                "exp": issued_at + 3600,
            },
            self.service_account_private_key,
            algorithm="RS256",
        )
        client = await get_client()
        token_response = await client.post(
            _OAUTH2_TOKEN_ENDPOINT,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
        global _cached_access_token, _cached_access_token_expires_at
        _cached_access_token = token_payload["access_token"]
        _cached_access_token_expires_at = time.monotonic() + int(
            token_payload.get("expires_in", 3600)
        )
        return _cached_access_token

    async def _access_token(self) -> str:
        if _cached_access_token is None or (
            time.monotonic() > _cached_access_token_expires_at - _ACCESS_TOKEN_EARLY_REFRESH_SECONDS
        ):
            return await self._fetch_access_token()
        return _cached_access_token

    async def upsert_lead_mirror(self, *, document_id: str, document: LeadMirrorDocument) -> None:
        stamped_document = dataclasses.replace(
            document, updated_at=datetime.now(timezone.utc).isoformat()
        )
        access_token = await self._access_token()
        response = await _request_with_retries(
            "PATCH",
            self._document_url(document_id),
            headers={"Authorization": f"Bearer {access_token}"},
            json={"fields": _document_fields(stamped_document)},
        )
        response.raise_for_status()

    async def remove_lead_mirror(self, document_id: str) -> None:
        access_token = await self._access_token()
        response = await _request_with_retries(
            "DELETE",
            self._document_url(document_id),
            headers={"Authorization": f"Bearer {access_token}"},
        )
        # Idempotent delete (ADR-0004): an already-gone document — the common
        # case for legacy-key cleanup once the backfill has converged — is
        # success, not a mirror failure.
        if response.status_code == 404:
            return
        response.raise_for_status()

    async def health_check(self) -> bool:
        try:
            await self._access_token()
            return True
        except Exception:  # noqa: BLE001 — readiness probes must never raise
            return False
