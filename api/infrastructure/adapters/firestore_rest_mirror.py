"""Firestore REST mirror adapter — the no-SDK write path (stack lock bans firebase-admin).

The BE authenticates to Firestore REST v1 with an OAuth2 service-account JWT
grant: an RS256 assertion signed with PyJWT from the configured client email +
private key, exchanged at Google's token endpoint for a short-lived access
token scoped to datastore. The token is cached module-level until shortly
before expiry so steady-state writes cost one PATCH, not a token round-trip.

PG stays the source of truth (hybrid D1): this adapter only writes the
denormalized lead snapshot consumed by realtime clients, and never reads back.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timezone
from typing import Any

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

_mirror_http_client: httpx.AsyncClient | None = None

_cached_access_token: str | None = None
_cached_access_token_expires_at: float = 0.0


async def get_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating it on first use."""
    global _mirror_http_client
    if _mirror_http_client is None or _mirror_http_client.is_closed:
        _mirror_http_client = httpx.AsyncClient(timeout=10.0)
    return _mirror_http_client


async def close_client() -> None:
    """Close the shared AsyncClient and drop the cached OAuth2 token."""
    global _mirror_http_client, _cached_access_token
    if _mirror_http_client is not None:
        await _mirror_http_client.aclose()
        _mirror_http_client = None
    _cached_access_token = None


def _document_fields(document: LeadMirrorDocument) -> dict[str, dict[str, Any]]:
    """Map the mirror dataclass into Firestore REST Value payloads, omitting Nones."""
    fields: dict[str, dict[str, Any]] = {
        "customer_id": {"stringValue": document.customer_id},
        "project_key": {"stringValue": document.project_key},
        "lead_status": {"stringValue": document.lead_status},
        "consent_service": {"booleanValue": document.consent_service},
        "consent_marketing": {"booleanValue": document.consent_marketing},
        "updated_at": {"timestampValue": document.updated_at},
    }
    optional_string_fields = {
        "display_name": document.display_name,
        "masked_phone": document.masked_phone,
        "assigned_sales_firebase_uid": document.assigned_sales_firebase_uid,
        "consent_recorded_at": document.consent_recorded_at,
        "last_customer_message_at": document.last_customer_message_at,
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

    def _document_url(self, customer_id: str) -> str:
        return (
            f"{self.rest_base_url}/projects/{self.project_id}"
            f"/databases/(default)/documents/leads/{customer_id}"
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
        _cached_access_token_expires_at = time.monotonic() + int(token_payload.get("expires_in", 3600))
        return _cached_access_token

    async def _access_token(self) -> str:
        if _cached_access_token is None or (
            time.monotonic() > _cached_access_token_expires_at - _ACCESS_TOKEN_EARLY_REFRESH_SECONDS
        ):
            return await self._fetch_access_token()
        return _cached_access_token

    async def upsert_lead_mirror(self, *, customer_id: str, document: LeadMirrorDocument) -> None:
        stamped_document = dataclasses.replace(
            document, updated_at=datetime.now(timezone.utc).isoformat()
        )
        access_token = await self._access_token()
        client = await get_client()
        response = await client.patch(
            self._document_url(customer_id),
            headers={"Authorization": f"Bearer {access_token}"},
            json={"fields": _document_fields(stamped_document)},
        )
        response.raise_for_status()

    async def remove_lead_mirror(self, customer_id: str) -> None:
        access_token = await self._access_token()
        client = await get_client()
        response = await client.delete(
            self._document_url(customer_id),
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()

    async def health_check(self) -> bool:
        try:
            await self._access_token()
            return True
        except Exception:  # noqa: BLE001 — readiness probes must never raise
            return False
