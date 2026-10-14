"""Offline contract tests for the Messenger permission probe endpoints."""

from __future__ import annotations

import json
from types import SimpleNamespace


import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.interfaces.api import messenger_permission_probe_routes as routes
from api.interfaces.api.deps.admin import AuthenticatedPrincipal, require_admin

PATH = "/api/integrations/messenger-bot/permission-probe"
PAGE_ID = "123456789012345"
API_VERSION = "v26.0"
BASE_URL = "https://graph.facebook.com"
TOKEN = "page-token-marker-xyz"
USER_TOKEN = "user-token-marker-abc"
APP_SECRET = "app-secret-marker-xyz"
PROBE_PSID = "psid-marker-789"
ATTACKER_PSID = "psid-attacker-000"
READ_ONLY_PERMISSIONS = {
    "pages_manage_metadata",
    "pages_read_engagement",
    "business_management",
    "pages_show_list",
}
SEND_PERMISSIONS = {"pages_messaging", "pages_utility_messaging"}
NOT_CONFIGURED = "MESSENGER_PROBE_PSID not set"


def _settings(**overrides):
    values = {
        "messenger_page_id": PAGE_ID,
        "messenger_page_access_token": TOKEN,
        "messenger_graph_api_version": API_VERSION,
        "messenger_api_base_url": BASE_URL,
        "messenger_probe_psid": PROBE_PSID,
        "messenger_probe_user_token": USER_TOKEN,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _app(monkeypatch, handler, *, settings=None):
    monkeypatch.setattr(routes, "get_settings", lambda: settings or _settings())
    monkeypatch.setattr(routes, "_transport_factory", lambda: httpx.MockTransport(handler))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: AuthenticatedPrincipal(
        firebase_uid="admin-uid", email=None, role="admin", sales_id=None
    )
    return TestClient(app)


def _ok_handler(calls):
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": []})

    return handle


def _by_permission(body):
    return {probe["permission"]: probe for probe in body["probes"]}


def test_unauthenticated_calls_are_rejected_before_any_graph_call(monkeypatch):
    calls = []
    monkeypatch.setattr(routes, "get_settings", lambda: _settings())
    monkeypatch.setattr(routes, "_transport_factory", lambda: httpx.MockTransport(_ok_handler(calls)))
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    assert client.post(PATH).status_code == 401
    assert client.get(PATH).status_code == 401
    assert calls == []


def test_unset_probe_psid_skips_message_probes_and_runs_the_rest(monkeypatch):
    calls = []
    client = _app(monkeypatch, _ok_handler(calls), settings=_settings(messenger_probe_psid=""))

    response = client.post(PATH)

    assert response.status_code == 200
    body = response.json()
    probes = _by_permission(body)
    assert len(body["probes"]) == 6
    assert body["sends_enabled"] is False
    for permission in SEND_PERMISSIONS:
        assert probes[permission]["status"] == "skipped"
        assert probes[permission]["summary"] == NOT_CONFIGURED
        assert probes[permission]["ok"] is False
    assert len(calls) == 4
    assert all(request.method == "GET" for request in calls)
    assert all("/messages" not in request.url.path for request in calls)


def test_configured_probe_psid_drives_both_message_shapes(monkeypatch):
    calls = []
    client = _app(monkeypatch, _ok_handler(calls))

    response = client.post(PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["sends_enabled"] is True
    probes = _by_permission(body)
    assert probes["pages_messaging"]["ok"] is True
    assert probes["pages_utility_messaging"]["ok"] is True
    sends = [request for request in calls if request.method == "POST"]
    assert len(sends) == 2
    for request in sends:
        assert request.url.path == f"/{API_VERSION}/{PAGE_ID}/messages"
    bodies = [json.loads(request.content) for request in sends]
    assert bodies[0] == {
        "recipient": {"id": PROBE_PSID},
        "messaging_type": "RESPONSE",
        "message": {"text": routes._PROBE_MESSAGE_TEXT},
    }
    assert bodies[1] == {
        "recipient": {"id": PROBE_PSID},
        "messaging_type": "MESSAGE_TAG",
        "tag": "NON_PROMOTIONAL_SUBSCRIPTION",
        "message": {"text": routes._PROBE_MESSAGE_TEXT},
    }


def test_request_supplied_recipient_is_ignored(monkeypatch):
    """A caller must never be able to redirect a real message to another person."""
    calls = []
    client = _app(monkeypatch, _ok_handler(calls))

    response = client.post(PATH, json={"psid": ATTACKER_PSID})

    assert response.status_code == 200
    sends = [request for request in calls if request.method == "POST"]
    assert len(sends) == 2
    for request in sends:
        assert json.loads(request.content)["recipient"] == {"id": PROBE_PSID}
    assert ATTACKER_PSID not in response.text


def test_one_failing_probe_does_not_abort_the_others(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations"):
            raise httpx.ConnectError(f"refused {request.url}")
        return httpx.Response(200, json={"data": []})

    client = _app(monkeypatch, handle)

    response = client.post(PATH)

    assert response.status_code == 200
    probes = _by_permission(response.json())
    assert len(probes) == 6
    assert probes["pages_read_engagement"]["ok"] is False
    assert probes["pages_read_engagement"]["summary"] == "transport error: ConnectError"
    assert probes["pages_manage_metadata"]["ok"] is True
    assert probes["business_management"]["ok"] is True
    assert probes["pages_messaging"]["ok"] is True
    assert probes["pages_utility_messaging"]["ok"] is True
    assert probes["pages_show_list"]["ok"] is True


def test_graph_error_body_is_reduced_to_status_and_code(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 190, "message": f"Invalid token {TOKEN} for {PROBE_PSID}"}},
        )

    client = _app(monkeypatch, handle)

    response = client.get(PATH)

    probe = _by_permission(response.json())["business_management"]
    assert probe["ok"] is False
    assert probe["http_status"] == 400
    assert probe["summary"] == "HTTP 400, Graph error code 190"


def test_response_never_contains_token_app_secret_or_recipient(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, json={"error": {"message": f"echo {TOKEN} {APP_SECRET} {PROBE_PSID}"}}
        )

    settings = _settings(messenger_app_secret=APP_SECRET)
    client = _app(monkeypatch, handle, settings=settings)

    post_text = client.post(PATH).text
    get_text = client.get(PATH).text

    for text in (post_text, get_text):
        assert TOKEN not in text
        assert APP_SECRET not in text
        assert PROBE_PSID not in text


def test_get_runs_only_read_only_probes(monkeypatch):
    calls = []
    client = _app(monkeypatch, _ok_handler(calls))

    response = client.get(PATH)

    assert response.status_code == 200
    body = response.json()
    probes = _by_permission(body)
    assert len(body["probes"]) == 6
    assert body["sends_enabled"] is True
    assert len(calls) == 4
    assert all(request.method == "GET" for request in calls)
    for permission in READ_ONLY_PERMISSIONS:
        assert probes[permission]["ok"] is True
    for permission in SEND_PERMISSIONS:
        assert probes[permission]["status"] == "skipped"
        assert probes[permission]["summary"] == "GET mode"


def test_required_flags_and_pages_show_list_is_optional(monkeypatch):
    client = _app(monkeypatch, _ok_handler([]))

    probes = _by_permission(client.get(PATH).json())

    assert probes["pages_show_list"]["required"] is False
    for permission in READ_ONLY_PERMISSIONS - {"pages_show_list"} | SEND_PERMISSIONS:
        assert probes[permission]["required"] is True


@pytest.mark.parametrize("probe_psid", ["", "   "])
def test_blank_probe_psid_is_treated_as_absent(monkeypatch, probe_psid):
    calls = []
    client = _app(
        monkeypatch, _ok_handler(calls), settings=_settings(messenger_probe_psid=probe_psid)
    )

    probes = _by_permission(client.post(PATH).json())

    assert probes["pages_messaging"]["summary"] == NOT_CONFIGURED
    assert len(calls) == 4


def test_user_scoped_probes_use_the_user_token(monkeypatch):
    """The Page token's /me is the Page, so user edges need their own token."""
    calls = []
    client = _app(monkeypatch, _ok_handler(calls))

    client.get(PATH)

    user_calls = [r for r in calls if "/me/" in r.url.path]
    page_calls = [r for r in calls if "/me/" not in r.url.path]
    assert len(user_calls) == 2
    for request in user_calls:
        assert request.url.params.get("access_token") == USER_TOKEN
    assert page_calls
    for request in page_calls:
        assert request.url.params.get("access_token") == TOKEN


def test_missing_user_token_reports_without_calling_that_edge(monkeypatch):
    calls = []
    client = _app(
        monkeypatch, _ok_handler(calls), settings=_settings(messenger_probe_user_token="")
    )

    probes = _by_permission(client.get(PATH).json())

    assert all("/me/" not in request.url.path for request in calls)
    assert probes["business_management"]["summary"] == "MESSENGER_PROBE_USER_TOKEN not set"
    assert probes["pages_show_list"]["summary"] == "MESSENGER_PROBE_USER_TOKEN not set"
    assert probes["pages_manage_metadata"]["ok"] is True


def test_missing_tokens_report_without_calling_graph(monkeypatch):
    calls = []
    client = _app(
        monkeypatch,
        _ok_handler(calls),
        settings=_settings(messenger_page_access_token="", messenger_probe_user_token=""),
    )

    probes = _by_permission(client.get(PATH).json())

    assert calls == []
    for permission in ("pages_manage_metadata", "pages_read_engagement"):
        assert probes[permission]["ok"] is False
        assert probes[permission]["summary"] == "page access token not configured"
    for permission in ("business_management", "pages_show_list"):
        assert probes[permission]["ok"] is False
        assert probes[permission]["summary"] == "MESSENGER_PROBE_USER_TOKEN not set"
