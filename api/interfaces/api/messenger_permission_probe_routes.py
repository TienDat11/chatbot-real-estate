"""Operator probes that make one real Graph call per Messenger permission.

Meta's App Review Testing tab counts only calls originating from the app, so
each probe issues the Graph request from this process using the configured
Page token. Probes run sequentially and never abort one another.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends

from api.infrastructure.config.config import Settings, get_settings
from api.interfaces.api.deps.admin import AuthenticatedPrincipal, require_admin

logger = logging.getLogger("api.messenger_permission_probe")

router = APIRouter(
    prefix="/api/integrations/messenger-bot",
    tags=["messenger-bot"],
)

_TIMEOUT_S = 10.0
_PROBE_MESSAGE_TEXT = "Permission probe"

# Test seam: None selects the real network stack; tests inject httpx.MockTransport.
_transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None


@dataclass(frozen=True)
class _ProbeSpec:
    permission: str
    method: str
    # Formatted with the configured Page id; never contains a credential.
    path_template: str
    query: tuple[tuple[str, str], ...]
    required: bool
    sends_message: bool = False
    message_tag: str | None = None
    # /me resolves to the Page under a Page token, so these user-scoped edges
    # need the separately configured user token or they answer (#100).
    user_scoped: bool = False


_PROBES: tuple[_ProbeSpec, ...] = (
    _ProbeSpec(
        permission="pages_manage_metadata",
        method="GET",
        path_template="{page_id}/messenger_profile",
        query=(("fields", "get_started"),),
        required=True,
    ),
    _ProbeSpec(
        permission="pages_read_engagement",
        method="GET",
        path_template="{page_id}/conversations",
        query=(("platform", "messenger"), ("limit", "1")),
        required=True,
    ),
    _ProbeSpec(
        permission="business_management",
        method="GET",
        path_template="me/businesses",
        query=(),
        required=True,
        user_scoped=True,
    ),
    _ProbeSpec(
        permission="pages_messaging",
        method="POST",
        path_template="{page_id}/messages",
        query=(),
        required=True,
        sends_message=True,
    ),
    _ProbeSpec(
        permission="pages_utility_messaging",
        method="POST",
        path_template="{page_id}/messages",
        query=(),
        required=True,
        sends_message=True,
        # Meta retired ACCOUNT_UPDATE (subcode 1893061, "unsupported message
        # tag") so that probe could never pass. NON_PROMOTIONAL_SUBSCRIPTION is
        # the remaining tag that Graph accepts without a prior opt-in.
        message_tag="NON_PROMOTIONAL_SUBSCRIPTION",
    ),
    _ProbeSpec(
        permission="pages_show_list",
        method="GET",
        path_template="me/accounts",
        query=(("limit", "1"),),
        required=False,
        user_scoped=True,
    ),
)


async def _call_graph(
    settings: Settings,
    spec: _ProbeSpec,
    body: dict[str, Any] | None,
) -> tuple[bool, int | None, str]:
    """Issue one Graph request; return (ok, http_status, values-free summary)."""
    page_id = settings.messenger_page_id
    if spec.user_scoped:
        token = settings.messenger_probe_user_token.strip()
        if not token:
            return False, None, "MESSENGER_PROBE_USER_TOKEN not set"
    else:
        token = settings.messenger_page_access_token
        if not token.strip():
            return False, None, "page access token not configured"
    if "{page_id}" in spec.path_template and not page_id.isdigit():
        return False, None, "page id not configured"

    base = settings.messenger_api_base_url.rstrip("/")
    path = spec.path_template.format(page_id=page_id)
    query = [("access_token", token), *spec.query]
    url = f"{base}/{settings.messenger_graph_api_version}/{path}?{urlencode(query)}"
    request = httpx.Request(
        spec.method,
        url,
        json=body,
        extensions={"timeout": httpx.Timeout(_TIMEOUT_S).as_dict()},
    )
    try:
        factory = _transport_factory
        context = (
            nullcontext(factory())
            if factory is not None
            else httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        )
        async with context as transport:
            response = await transport.handle_async_request(request)
            try:
                status = response.status_code
                await response.aread()
                try:
                    payload = response.json()
                except Exception:
                    payload = None
            finally:
                await response.aclose()
    except Exception as exc:
        # Transport errors can embed the token-bearing URL; only the type leaves here.
        return False, None, f"transport error: {type(exc).__name__}"

    if 200 <= status < 300:
        return True, status, f"HTTP {status}"
    error = payload.get("error") if isinstance(payload, dict) else None
    error_code = error.get("code") if isinstance(error, dict) else None
    if isinstance(error_code, int) and not isinstance(error_code, bool):
        return False, status, f"HTTP {status}, Graph error code {error_code}"
    return False, status, f"HTTP {status}"


def _probe_result(spec: _ProbeSpec, settings: Settings, path_page_id: str) -> dict[str, Any]:
    path = spec.path_template.format(page_id=path_page_id)
    return {
        "permission": spec.permission,
        "endpoint": f"{spec.method} /{settings.messenger_graph_api_version}/{path}",
        "required": spec.required,
    }


async def _run_probes(*, include_sends: bool) -> dict[str, Any]:
    settings = get_settings()
    page_id = settings.messenger_page_id
    # The recipient comes from configuration only. A request-supplied PSID would
    # let any admin token holder deliver a real message to an arbitrary person.
    allowed_psid = settings.messenger_probe_psid.strip()
    probes: list[dict[str, Any]] = []
    for spec in _PROBES:
        result = _probe_result(spec, settings, page_id)
        if spec.sends_message and not include_sends:
            result.update({"ok": False, "status": "skipped", "summary": "GET mode"})
        elif spec.sends_message and not allowed_psid:
            result.update(
                {"ok": False, "status": "skipped", "summary": "MESSENGER_PROBE_PSID not set"}
            )
        else:
            body = None
            if spec.sends_message:
                message: dict[str, Any] = {"text": _PROBE_MESSAGE_TEXT}
                body = {"recipient": {"id": allowed_psid}, "message": message}
                if spec.message_tag is None:
                    body["messaging_type"] = "RESPONSE"
                else:
                    body["messaging_type"] = "MESSAGE_TAG"
                    body["tag"] = spec.message_tag
            try:
                ok, status, summary = await _call_graph(settings, spec, body)
            except Exception as exc:
                ok, status, summary = False, None, f"probe error: {type(exc).__name__}"
            result.update({"ok": ok, "http_status": status, "summary": summary})
            logger.info(
                "messenger permission probe",
                extra={"permission": spec.permission, "ok": ok, "http_status": status},
            )
        probes.append(result)
    return {
        "probes": probes,
        "page_id": page_id,
        "api_version": settings.messenger_graph_api_version,
        "sends_enabled": bool(allowed_psid),
    }


@router.post("/permission-probe")
async def messenger_permission_probe(
    _: AuthenticatedPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    """Run every permission probe, including the message-sending pair.

    The recipient is fixed by MESSENGER_PROBE_PSID; when that is unset the two
    send probes report as skipped instead of guessing a recipient.
    """
    return await _run_probes(include_sends=True)


@router.get("/permission-probe")
async def messenger_permission_probe_read_only(
    _: AuthenticatedPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    """Run the read-only probes; message-sending probes are reported as skipped."""
    return await _run_probes(include_sends=False)
