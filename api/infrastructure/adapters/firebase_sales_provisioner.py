"""Firebase REST sales provisioning adapter."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt

from api.application.ports.sales_provisioning import (
    SalesProvisioningError,
    SalesProvisioningResult,
)

_OAUTH2_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_IDENTITY_TOOLKIT_BASE = "https://identitytoolkit.googleapis.com/v1"
_FIRESTORE_BASE = "https://firestore.googleapis.com/v1"
_SCOPES = (
    "https://www.googleapis.com/auth/identitytoolkit https://www.googleapis.com/auth/cloud-platform"
)


class FirebaseSalesProvisioner:
    def __init__(
        self,
        *,
        project_id: str,
        client_email: str,
        private_key: str,
        rest_base_url: str = _FIRESTORE_BASE,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.project_id = project_id
        self.client_email = client_email
        self.private_key = private_key.replace("\\n", "\n")
        self.rest_base_url = rest_base_url.rstrip("/")
        self._http_client = http_client

    def _profile_url(self, firebase_uid: str) -> str:
        return (
            f"{self.rest_base_url}/projects/{self.project_id}/databases/"
            f"(default)/documents/sales/{firebase_uid}"
        )

    async def _token(self) -> str:
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": self.client_email,
                "scope": _SCOPES,
                "aud": _OAUTH2_TOKEN_ENDPOINT,
                "iat": now,
                "exp": now + 3600,
            },
            self.private_key,
            algorithm="RS256",
        )
        client = self._http_client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await client.post(
                _OAUTH2_TOKEN_ENDPOINT,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
            response.raise_for_status()
            token = response.json().get("access_token")
            if not token:
                raise SalesProvisioningError("Firebase OAuth token response was incomplete")
            return str(token)
        except SalesProvisioningError:
            raise
        except Exception as exc:
            raise SalesProvisioningError("Firebase OAuth authentication failed") from exc
        finally:
            if self._http_client is None:
                await client.aclose()

    async def _get_profile(self, *, firebase_uid: str, token: str) -> dict[str, Any] | None:
        client = self._http_client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await client.get(
                self._profile_url(firebase_uid), headers={"Authorization": f"Bearer {token}"}
            )
            if response.status_code == 404:
                return None
            if response.is_error:
                raise SalesProvisioningError("Firestore sales profile read failed")
            return dict(response.json().get("fields") or {})
        finally:
            if self._http_client is None:
                await client.aclose()

    async def _set_claim(self, *, firebase_uid: str, token: str, role: str | None) -> None:
        client = self._http_client or httpx.AsyncClient(timeout=15.0)
        try:
            attributes = {"role": role} if role else {}
            response = await client.post(
                f"{_IDENTITY_TOOLKIT_BASE}/accounts:update",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "localId": firebase_uid,
                    "customAttributes": json.dumps(attributes, separators=(",", ":")),
                },
            )
            if response.is_error:
                raise SalesProvisioningError("Firebase sales claim update failed")
        finally:
            if self._http_client is None:
                await client.aclose()

    async def _write_profile(
        self, *, firebase_uid: str, token: str, fields: dict[str, Any]
    ) -> None:
        client = self._http_client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await client.patch(
                self._profile_url(firebase_uid),
                headers={"Authorization": f"Bearer {token}"},
                json={"fields": fields},
            )
            if response.is_error:
                raise SalesProvisioningError("Firestore sales profile write failed")
        finally:
            if self._http_client is None:
                await client.aclose()

    @staticmethod
    def _merged_profile(
        existing: dict[str, Any] | None,
        *,
        firebase_uid: str,
        full_name: str | None,
        email: str | None,
        is_active: bool,
    ) -> dict[str, Any]:
        fields = dict(existing or {})
        fields.update(
            {
                "firebase_uid": {"stringValue": firebase_uid},
                "role": {"stringValue": "sales"},
                "is_active": {"booleanValue": is_active},
                "updated_at": {
                    "timestampValue": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                },
            }
        )
        if full_name is not None:
            fields["display_name"] = {"stringValue": full_name}
        if email is not None:
            fields["email"] = {"stringValue": email}
        return fields

    async def provision(
        self, *, firebase_uid: str, full_name: str, email: str | None = None
    ) -> SalesProvisioningResult:
        token = await self._token()
        previous = await self._get_profile(firebase_uid=firebase_uid, token=token)
        pre_existing_active = bool(
            previous and previous.get("is_active", {}).get("booleanValue") is True
        )
        try:
            await self._set_claim(firebase_uid=firebase_uid, token=token, role="sales")
            await self._write_profile(
                firebase_uid=firebase_uid,
                token=token,
                fields=self._merged_profile(
                    previous,
                    firebase_uid=firebase_uid,
                    full_name=full_name,
                    email=email,
                    is_active=True,
                ),
            )
        except Exception as original:
            reconciliation_failed = False
            try:
                if previous is not None:
                    await self._write_profile(
                        firebase_uid=firebase_uid, token=token, fields=previous
                    )
                else:
                    await self._write_profile(
                        firebase_uid=firebase_uid,
                        token=token,
                        fields=self._merged_profile(
                            None,
                            firebase_uid=firebase_uid,
                            full_name=None,
                            email=None,
                            is_active=False,
                        ),
                    )
                await self._set_claim(
                    firebase_uid=firebase_uid,
                    token=token,
                    role="sales" if pre_existing_active else None,
                )
            except Exception:
                reconciliation_failed = True
            if isinstance(original, SalesProvisioningError):
                original.reconciliation_failed = reconciliation_failed
                raise
            raise SalesProvisioningError(
                "Firebase sales provisioning failed", reconciliation_failed=reconciliation_failed
            ) from original
        return SalesProvisioningResult(pre_existing_active=pre_existing_active)

    async def revoke(self, *, firebase_uid: str) -> None:
        token = await self._token()
        previous = await self._get_profile(firebase_uid=firebase_uid, token=token)
        if previous is None:
            previous = self._merged_profile(
                None, firebase_uid=firebase_uid, full_name=None, email=None, is_active=True
            )
        try:
            # Preserve every profile field while closing the Firestore gate first.
            await self._write_profile(
                firebase_uid=firebase_uid,
                token=token,
                fields=self._merged_profile(
                    previous, firebase_uid=firebase_uid, full_name=None, email=None, is_active=False
                ),
            )
            await self._set_claim(firebase_uid=firebase_uid, token=token, role=None)
        except Exception as original:
            # A failed revoke is compensated back to the prior active state so
            # callers can safely keep PG active; the original exception wins.
            try:
                await self._write_profile(firebase_uid=firebase_uid, token=token, fields=previous)
                await self._set_claim(firebase_uid=firebase_uid, token=token, role="sales")
            except Exception:
                pass
            if isinstance(original, SalesProvisioningError):
                raise
            raise SalesProvisioningError("Firebase sales revocation failed") from original
