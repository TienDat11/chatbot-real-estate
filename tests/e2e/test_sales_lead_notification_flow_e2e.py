"""Sales realtime-lead E2E, no-LLM core (notification -> reveal loop).

Covers, against locally running FE (:3000) + BE (:8000):
  1. one consented lead via POST /api/lead  -> exactly one 201;
  2. Firestore mirror is masked (no raw phone anywhere in the document);
  3. while sales is on /sales/chat/project/camellia?sessionId=<uuid> the
     incoming lead raises the antd toast, the unread bell badge and the
     aria-live unread announcement from the production assigned stream;
  4. the notification navigates to /sales/leads?lead=<id> and the deep link
     opens the detail drawer;
  5. the reveal button is enabled immediately and shows the full phone with
     Call/Copy actions;
  6. GET /api/crm/leads/<id>/conversation returns 200 with an empty transcript
     for a lead that never chatted (and 401 without a bearer);
  7. the canonical project/session URL stays on the /sales/chat/project/* path.

Credentials are loaded ONLY from the ignored .env into process env and are
never logged; failure messages are PII-free (no phone, email, uid, token).
Screenshots are intentionally NOT taken (the reveal step shows the raw phone).

Run: python -m pytest tests/e2e/test_sales_lead_notification_flow_e2e.py -m e2e -s
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest
from playwright.sync_api import Page, expect, sync_playwright

from api.application.services.lead_mirror_service import compute_lead_document_id
from api.application.services.lead_service import mask_phone
from api.infrastructure.config.config import get_settings

pytestmark = [pytest.mark.e2e]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BE = "http://127.0.0.1:8000"
_FE = os.environ.get("E2E_BASE_URL", "http://localhost:3000").rstrip("/")

# Only these keys are lifted from the ignored .env into process env; values are
# never printed, never persisted anywhere else (process-env-only contract).
_ENV_KEYS_NEEDED = (
    "SALES_LOGIN_EMAIL",
    "SALES_LOGIN_PASSWORD",
    "FIREBASE_WEB_API_KEY",
    "FIREBASE_PROJECT_ID",
    "FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL",
    "FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE",
)


def _load_env_into_process() -> None:
    for line in (_REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _ENV_KEYS_NEEDED and key not in os.environ:
            os.environ[key] = value.strip().strip('"')


_load_env_into_process()

_FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/"
    f"{os.environ.get('FIREBASE_PROJECT_ID', '')}/databases/(default)/documents"
)

# Unique in-process tag prefix: identifies EVERY lead this suite created so the
# cleanup is exact even across fixture instances (no pattern wider than QA E2E
# leads created by this file during this run).
_TAG_PREFIX = f"QA E2E {uuid.uuid4().hex[:6]}"


def _assert_local_url(url: str) -> None:
    parsed = urlparse(url)
    assert parsed.scheme in {"http", "https"}, "E2E target must be http(s)"
    assert parsed.hostname in {"localhost", "127.0.0.1", "::1"}, (
        "E2E target outside localhost allowlist"
    )


_assert_local_url(_BE)
_assert_local_url(_FE)


# --------------------------------------------------------------------------- #
# Credentials / tokens (process env only, never logged)
# --------------------------------------------------------------------------- #


def _sales_credentials() -> tuple[str, str]:
    email = os.environ.get("E2E_SALES_EMAIL") or os.environ.get("SALES_LOGIN_EMAIL") or ""
    password = os.environ.get("E2E_SALES_PASSWORD") or os.environ.get("SALES_LOGIN_PASSWORD") or ""
    if not email or not password:
        pytest.skip("sales credentials absent from ignored .env")
    return email, password


_ID_TOKEN_CACHE: dict[str, str] = {}


def sales_id_token() -> str:
    """Firebase REST password sign-in; the idToken is the API bearer."""
    if "token" in _ID_TOKEN_CACHE:
        return _ID_TOKEN_CACHE["token"]
    email, password = _sales_credentials()
    api_key = os.environ.get("FIREBASE_WEB_API_KEY", "")
    assert api_key, "FIREBASE_WEB_API_KEY missing from .env"
    response = httpx.post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}",
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=15,
    )
    assert response.status_code == 200, f"sales sign-in failed with HTTP {response.status_code}"
    body = response.json()
    _ID_TOKEN_CACHE["token"] = body["idToken"]
    _ID_TOKEN_CACHE["uid"] = body["localId"]
    return _ID_TOKEN_CACHE["token"]


def sales_uid() -> str:
    sales_id_token()
    return _ID_TOKEN_CACHE["uid"]


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {sales_id_token()}", "Content-Type": "application/json"}


# --------------------------------------------------------------------------- #
# Firestore read/delete via REST (service account, token in-memory only)
# --------------------------------------------------------------------------- #


def _firestore_access_token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account as sa

    private_key = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY", "").replace("\\n", "\n")
    creds = sa.Credentials.from_service_account_info(
        {
            "client_email": os.environ.get("FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL", ""),
            "private_key": private_key,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=["https://www.googleapis.com/auth/datastore"],
    )
    creds.refresh(Request())
    assert creds.token, "firestore access token mint failed"
    return creds.token


def firestore_get(document_id: str) -> dict:
    response = httpx.get(
        f"{_FIRESTORE_BASE}/leads/{document_id}",
        headers={"Authorization": f"Bearer {_firestore_access_token()}"},
        timeout=15,
    )
    assert response.status_code == 200, f"firestore read failed with HTTP {response.status_code}"
    return response.json()


def firestore_delete(document_id: str) -> None:
    httpx.delete(
        f"{_FIRESTORE_BASE}/leads/{document_id}",
        headers={"Authorization": f"Bearer {_firestore_access_token()}"},
        timeout=15,
    )


# --------------------------------------------------------------------------- #
# PG cleanup (exact lead / exact session only; sales rows untouched)
# --------------------------------------------------------------------------- #


def _pg_cleanup(lead_id: int, session_id: str) -> None:
    async def _run() -> None:
        conn = await asyncpg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            database=os.environ["POSTGRES_DATABASE"],
            timeout=10,
        )
        try:
            await conn.execute("DELETE FROM staff_audit_log WHERE lead_id = $1", lead_id)
            await conn.execute("DELETE FROM sales_assignment_log WHERE lead_id = $1", lead_id)
            await conn.execute("DELETE FROM sales_notification_reads WHERE lead_id = $1", lead_id)
            await conn.execute("DELETE FROM leads WHERE id = $1", lead_id)
            await conn.execute("DELETE FROM chat_messages WHERE session_id = $1", session_id)
            await conn.execute("DELETE FROM chat_sessions WHERE session_id = $1", session_id)
        finally:
            await conn.close()

    asyncio.run(_run())


def _reset_ip_lead_counter() -> None:
    """Drop ONLY this suite's own IP-brake rows (127.0.0.1, kind='lead').

    The endpoint's ip_rate_limit window is 1h and every POST here comes from
    127.0.0.1, so consecutive runs would otherwise brick each other with 429.
    No other ip/kind combination is touched.
    """

    async def _run() -> None:
        conn = await asyncpg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            database=os.environ["POSTGRES_DATABASE"],
            timeout=10,
        )
        try:
            await conn.execute(
                "DELETE FROM ip_rate_limit WHERE ip = $1 AND kind = 'lead'",
                "127.0.0.1",
            )
        finally:
            await conn.close()

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# The tagged-lead factory (each call = one POST /api/lead -> one 201)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def lead_factory():
    created: list[dict] = []
    # Counters left by PRIOR runs must not brick this one with 429.
    _reset_ip_lead_counter()

    def _create(session_id: str | None = None) -> dict:
        tag = f"{_TAG_PREFIX} {uuid.uuid4().hex[:8]}"
        phone = f"090{int(uuid.uuid4().int % 10_000_000):07d}"
        session_id = session_id or str(uuid.uuid4())
        device_id = str(uuid.uuid4())
        response = httpx.post(
            f"{_BE}/api/lead",
            json={
                "project_key": "camellia",
                "session_id": session_id,
                "device_id": device_id,
                "name": tag,
                "phone": phone,
                "consent": True,
                "note": f"e2e-tag {tag}",
            },
            headers={"X-Device-Id": device_id},
            timeout=15,
        )
        assert response.status_code == 201, (
            f"POST /api/lead expected 201, got {response.status_code}"
        )
        lead_id = response.json()["lead_id"]
        lead = {
            "lead_id": lead_id,
            "phone": phone,
            "name": tag,
            "session_id": session_id,
            "device_id": device_id,
            "doc_id": compute_lead_document_id(lead_id),
        }
        created.append(lead)
        return lead

    yield _create

    # Exact cleanup: only leads this suite created + their sessions; the sales
    # account, every other lead and all assignments stay untouched.
    for lead in created:
        firestore_delete(lead["doc_id"])
        try:
            _pg_cleanup(lead["lead_id"], lead["session_id"])
        except Exception:  # noqa: BLE001 — cleanup must not mask the test outcome
            pass
    _reset_ip_lead_counter()


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #


def _login_sales(page: Page) -> None:
    email, password = _sales_credentials()
    page.goto(f"{_FE}/login", wait_until="domcontentloaded")
    expect(page.get_by_text("Đăng nhập hệ thống")).to_be_visible(timeout=20_000)
    page.get_by_label("Email").fill(email)
    page.get_by_label("Mật khẩu").fill(password)
    page.get_by_role("button", name="Đăng nhập").click()
    expect(page).to_have_url(re.compile(r"/sales/leads"), timeout=25_000)


def _wait_url_session_stable(page: Page, canonical_re: re.Pattern[str]) -> str:
    """Wait until the chat URL's session id stops being rewritten at mount.

    The app normalizes the deep-linked session at mount (the sessionStorage-
    backed id wins), so the URL can flip shortly after load. Poll until two
    consecutive reads agree, then return the settled session id.
    """
    previous = canonical_re.search(page.url)
    assert previous is not None, "chat URL lost its canonical project/session shape"
    for _ in range(8):
        time.sleep(1.0)
        current = canonical_re.search(page.url)
        assert current is not None, "chat URL lost its canonical project/session shape"
        if current.group(1) == previous.group(1):
            return current.group(1)
        previous = current
    raise AssertionError("chat session URL never stabilized after mount")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_preferred_uid_maps_to_logged_in_sales():
    # Assignment correctness precondition: the BE's SALES_PREFERRED_FIREBASE_UID
    # must resolve to the account we log in with, otherwise the realtime stream
    # (uid-filtered) would never show the lead.
    assert get_settings().sales_preferred_firebase_uid == sales_uid(), (
        "SALES_PREFERRED_FIREBASE_UID does not match the .env sales account"
    )


def test_firestore_mirror_is_masked_and_assigned(lead_factory):
    lead = lead_factory()
    doc = firestore_get(lead["doc_id"])
    fields = doc["fields"]
    # Masked, never raw.
    assert fields["masked_phone"]["stringValue"] == mask_phone(lead["phone"])
    serialized = json.dumps(doc)
    assert lead["phone"] not in serialized, "raw phone leaked into Firestore mirror document"
    # Assignment reaches the logged-in sales identity (preferred-uid override).
    assert fields["assigned_sales_firebase_uid"]["stringValue"] == sales_uid()
    assert fields["lead_id"]["integerValue"] == str(lead["lead_id"])
    assert fields["consent_service"]["booleanValue"] is True
    assert fields["project_key"]["stringValue"] == "camellia"


def test_conversation_endpoint_empty_transcript_and_auth_gate(lead_factory):
    lead = lead_factory()
    # No chat happened for this lead: 200 with an empty transcript.
    response = httpx.get(
        f"{_BE}/api/crm/leads/{lead['lead_id']}/conversation",
        headers=_auth_headers(),
        timeout=15,
    )
    assert response.status_code == 200, f"conversation expected 200, got {response.status_code}"
    assert response.json()["messages"] == []
    # Negative space: no token -> 401, never a transcript.
    anon = httpx.get(f"{_BE}/api/crm/leads/{lead['lead_id']}/conversation", timeout=15)
    assert anon.status_code == 401


def test_unread_notification_api_lists_new_lead(lead_factory):
    lead = lead_factory()
    response = httpx.get(
        f"{_BE}/api/sales/notifications?status=unread&limit=100",
        headers=_auth_headers(),
        timeout=15,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["unread_count"] >= 1
    assert any(int(item["lead_id"]) == lead["lead_id"] for item in body["items"])


def test_sales_realtime_toast_badge_reveal_and_canonical_route(lead_factory):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        try:
            _login_sales(page)
            # The lead board's live badge ("Trực tiếp") proves the CURRENT
            # subscription instance delivered its first snapshot, and the
            # unsuffixed bell label proves the unread load finished — both
            # BEFORE any lead exists (condition waits, no fixed sleeps).
            expect(page.get_by_text("Trực tiếp")).to_be_visible(timeout=30_000)
            expect(
                page.get_by_role("button", name=re.compile(r"^Thông báo(?:, \d+ chưa đọc)?$"))
            ).to_be_visible(timeout=30_000)

            # Full load of the canonical project-scoped sales chat URL. The app
            # normalizes the deep-linked session at mount (sessionStorage-backed
            # id wins), so wait for the URL to stabilize and bind the lead to
            # the session the on-screen chat actually uses.
            page.goto(
                f"{_FE}/sales/chat/project/camellia?sessionId={uuid.uuid4()}",
                wait_until="domcontentloaded",
            )
            canonical_re = re.compile(r"/sales/chat/project/camellia\?sessionId=([0-9a-f-]{36})$")
            expect(page).to_have_url(
                re.compile(r"/sales/chat/project/camellia\?sessionId=[0-9a-f-]{36}$"),
                timeout=25_000,
            )
            session_id = _wait_url_session_stable(page, canonical_re)
            expect(page.get_by_role("navigation", name="Điều hướng chính")).to_have_count(1)
            expect(
                page.get_by_role("button", name=re.compile(r"^Thông báo(?:, \d+ chưa đọc)?$"))
            ).to_be_visible(timeout=30_000)

            lead = lead_factory(session_id=session_id)  # exactly one POST -> one 201

            # Realtime: toast + unread badge + aria-live from the production
            # assigned mirror stream while on the chat page.
            expect(page.get_by_text("Lead mới")).to_be_visible(timeout=45_000)
            bell = page.get_by_role("button", name=re.compile(r"Thông báo, \d+ chưa đọc"))
            expect(bell).to_be_visible(timeout=30_000)
            expect(page.get_by_text(re.compile(r"^\d+ thông báo chưa đọc$"))).to_be_attached(
                timeout=30_000
            )

            # Notification panel lists this exact lead and navigates to it.
            bell.click()
            link = page.get_by_role("link", name=lead["name"])
            expect(link).to_be_visible(timeout=15_000)
            link.click()
            expect(page).to_have_url(f"{_FE}/sales/leads?lead={lead['lead_id']}", timeout=20_000)
            # The ?lead= deep link opens the detail drawer directly.
            drawer = page.locator(".ant-drawer").filter(has_text="Chi tiết khách hàng")
            expect(drawer).to_be_visible(timeout=30_000)
            # Keep the drawer open; the notification popover may remain above it
            # after navigation, so target the drawer control without reopening a
            # table row through the mask.
            reveal = drawer.get_by_role("button", name="Hiện số đầy đủ")
            expect(reveal).to_be_visible(timeout=20_000)
            # Enabled immediately — no manual phone lookup required for own leads.
            expect(reveal).to_be_enabled(timeout=3_000)
            reveal.evaluate("(button) => button.click()")
            call_link = page.get_by_role("link", name="Gọi số điện thoại khách hàng")
            expect(call_link).to_be_visible(timeout=20_000)
            # Full phone shown; asserted without printing it.
            assert call_link.inner_text() == lead["phone"], "revealed phone mismatch"
            expect(
                drawer.get_by_role("button", name="Sao chép số điện thoại", exact=True)
            ).to_be_visible(timeout=10_000)

            # Close the detail drawer — its mask blocks the shell header used
            # by the canonical-switch leg below.
            drawer = page.get_by_role("dialog")
            page.keyboard.press("Escape")
            try:
                expect(drawer).to_be_hidden(timeout=5_000)
            except AssertionError:
                drawer.get_by_role("button", name="Close").click()
                expect(drawer).to_be_hidden(timeout=10_000)
            shell_nav = page.get_by_role("navigation", name="Điều hướng chính")
            expect(shell_nav).to_be_visible(timeout=10_000)
            # The nav label differs per surface ("Tư vấn" on chat, "Chat bán
            # hàng" on the CRM board); target the /sales/chat link by either.
            chat_nav_link = shell_nav.get_by_role("link", name=re.compile(r"Tư vấn|Chat bán hàng"))
            expect(chat_nav_link).to_be_visible(timeout=10_000)

            # Canonical project switch on the sales surface: the sales shell
            # owns the chrome (no customer picker button), so switching happens
            # through ROUTES. (a) A client-side detour to the bare sales chat
            # and back re-applies the scoped project via the route-sync effect
            # and must land on the canonical /sales/chat/project/* URL with a
            # session — never the customer /project/* path or /train. (The
            # switch routine intentionally mints a fresh session per project.)
            chat_nav_link.click()
            expect(page).to_have_url(re.compile(r"/sales/chat$"), timeout=20_000)
            # Back through the notification-detour entry, then to the camellia
            # chat entry: the route-sync effect re-applies the scoped project
            # (minting a fresh session by design) and the URL must land on the
            # canonical /sales/chat/project/* shape with a session.
            page.go_back()
            expect(page).to_have_url(re.compile(r"/sales/leads(?:\?lead=\d+)?$"), timeout=20_000)
            page.goto(
                f"{_FE}/sales/chat/project/camellia?sessionId={uuid.uuid4()}",
                wait_until="domcontentloaded",
            )
            expect(page).to_have_url(
                re.compile(r"/sales/chat/project/camellia\?sessionId=[0-9a-f-]{36}$"),
                timeout=25_000,
            )
            expect(page.get_by_role("navigation", name="Điều hướng chính")).to_have_count(1)
            _wait_url_session_stable(page, canonical_re)
            # (b) The sibling project's canonical URL loads and REMAINS on the
            # canonical sales path with a session — no bounce to /project/*.
            page.goto(
                f"{_FE}/sales/chat/project/soleil?sessionId={uuid.uuid4()}",
                wait_until="domcontentloaded",
            )
            expect(page).to_have_url(
                re.compile(r"/sales/chat/project/soleil\?sessionId=[0-9a-f-]{36}$"), timeout=25_000
            )
            _wait_url_session_stable(
                page, re.compile(r"/sales/chat/project/soleil\?sessionId=([0-9a-f-]{36})$")
            )
            expect(page.get_by_role("navigation", name="Điều hướng chính")).to_have_count(1)
        finally:
            context.close()
            browser.close()
