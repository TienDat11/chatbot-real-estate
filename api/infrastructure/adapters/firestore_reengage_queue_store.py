"""Firestore REST adapter for the re-approach queue (story 9.4 / ISSUE-10).

Same no-SDK auth path as firestore_rest_mirror.py: an RS256 service-account
JWT grant exchanged for a short-lived datastore-scoped OAuth2 token (the stack
lock bans firebase-admin). Kept as a separate adapter instead of sharing the
mirror's token cache so the two write paths can be configured, rotated, and
failure-isolated independently.

Authorization follows the server-writer model of docs/adr/0003: REST access is
governed by Google IAM (roles/datastore.user on the service account) and never
by client-facing security rules.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Sequence

import httpx
import jwt

from api.application.ports.reengage_queue import (
    ReengageQueueEntry,
    ReengageQueueNotConfiguredError,
)

logger = logging.getLogger("api.adapters.firestore_reengage_queue_store")

_ACCESS_TOKEN_EARLY_REFRESH_SECONDS = 60.0
_OAUTH2_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_FIRESTORE_DATASTORE_SCOPE = "https://www.googleapis.com/auth/datastore"

_queue_http_client: httpx.AsyncClient | None = None
_cached_access_token: str | None = None
_cached_access_token_expires_at: float = 0.0


async def get_client() -> httpx.AsyncClient:
    global _queue_http_client
    if _queue_http_client is None or _queue_http_client.is_closed:
        _queue_http_client = httpx.AsyncClient(timeout=10.0)
    return _queue_http_client


async def close_client() -> None:
    global _queue_http_client, _cached_access_token
    if _queue_http_client is not None:
        await _queue_http_client.aclose()
        _queue_http_client = None
    _cached_access_token = None


def queue_document_id(customer_id: str, project_key: str) -> str:
    """Deterministic document id — re-running a match overwrites, never duplicates."""
    return f"{customer_id}_{project_key}"


def _document_fields(entry: ReengageQueueEntry) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {
        "customer_id": {"stringValue": entry.customer_id},
        "project_key": {"stringValue": entry.project_key},
        "similarity_score": {"doubleValue": entry.similarity_score},
        "attempt_count": {"integerValue": str(entry.attempt_count)},
    }
    if entry.rejection_reason is not None:
        fields["rejection_reason"] = {"stringValue": entry.rejection_reason}
    if entry.budget_vnd is not None:
        fields["budget_vnd"] = {"integerValue": str(entry.budget_vnd)}
    return fields


class FirestoreReengageQueueStore:
    """Writes reengage_queue documents to Firestore over REST v1."""

    def __init__(
        self,
        *,
        project_id: str,
        service_account_client_email: str,
        service_account_private_key: str,
        rest_base_url: str,
    ) -> None:
        if not service_account_client_email or not service_account_private_key:
            raise ReengageQueueNotConfiguredError(
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
            f"/databases/(default)/documents/reengage_queue/{document_id}"
        )

    def _collection_url(self) -> str:
        return (
            f"{self.rest_base_url}/projects/{self.project_id}"
            f"/databases/(default)/documents/reengage_queue"
        )

    async def _fetch_access_token(self) -> str:
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
        _cached_access_token_expires_at = time.monotonic() + int(token_payload.get("expires_in", 3600))
        return _cached_access_token

    async def _access_token(self) -> str:
        if _cached_access_token is None or (
            time.monotonic() > _cached_access_token_expires_at - _ACCESS_TOKEN_EARLY_REFRESH_SECONDS
        ):
            return await self._fetch_access_token()
        return _cached_access_token

    async def save_queue_entries(self, entries: Sequence[ReengageQueueEntry]) -> None:
        queued_at = datetime.now(timezone.utc).isoformat()
        access_token = await self._access_token()
        client = await get_client()
        for entry in entries:
            response = await client.patch(
                self._document_url(queue_document_id(entry.customer_id, entry.project_key)),
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "fields": {
                        **_document_fields(entry),
                        "queued_at": {"timestampValue": queued_at},
                    }
                },
            )
            response.raise_for_status()

    async def load_attempt_counts_by_customer_id(self) -> dict[str, int]:
        access_token = await self._access_token()
        client = await get_client()
        response = await client.get(
            self._collection_url(),
            params={"pageSize": 300},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        counts: dict[str, int] = {}
        for document in response.json().get("documents", []):
            fields = document.get("fields", {})
            customer_id = fields.get("customer_id", {}).get("stringValue")
            if customer_id:
                counts[customer_id] = counts.get(customer_id, 0) + 1
        return counts
