"""Two-context customer-to-sales realtime lead E2E (synced, PII-safe).

Run with the FE and BE running locally:
    python -m pytest tests/e2e/test_customer_to_sales_loop_e2e.py -m e2e -s

Credentials are read only from E2E_SALES_EMAIL/E2E_SALES_PASSWORD. The test
never logs credentials, phone numbers, firebase tokens, or firebase uids; every
failure message is PII-free, assert clauses carry fixed text only.

Synchronization contract: UI steps wait on user-visible states with Playwright
auto-waiting (``expect``). There are no fixed sleeps and no UI bypass (no
evaluate/force clicks). The only retry loops are condition-polling a live API
with a hard deadline. When chat readiness cannot be established, the failure
includes a bounded diagnostics snapshot (URL, composer state, wall/CTA
visibility, HTTP 5xx path list, pageerror type names) — never bodies.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest
from playwright.sync_api import Page, expect, sync_playwright

from api.application.services.lead_mirror_service import compute_lead_document_id
from api.application.services.lead_service import mask_phone
from api.infrastructure.config.config import get_settings
from api.infrastructure.dependencies import get_realtime_lead_mirror

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:3000").rstrip("/")

_CHAT_READY_TIMEOUT_MS = 30_000
_ENTRY_TIMEOUT_MS = 30_000
_TURN_FINISHED_TIMEOUT_MS = 120_000
_SALES_VISIBILITY_TIMEOUT_MS = 30_000
_LOGIN_REDIRECT_TIMEOUT_MS = 20_000

# Visible entry points (user-visible only).
_CTA_RE = re.compile("Nhận bảng giá \\+ ưu đãi", re.I)
_WALL_CTA_RE = re.compile("Để lại số điện thoại nhận thêm lượt tư vấn")
_CONSENT_RE = re.compile("Tôi đồng ý nhận cuộc gọi", re.I)
# Composer idle placeholder (apps/web/src/lib/constants.ts): the exact
# glyphs must match the FE constant; the send button is NOT a completion
# signal (input is cleared on send, see Composer.tsx canSend), which is
# why the stream-finished wait keys off this placeholder instead.
_PLACEHOLDER_IDLE = "Nhập câu hỏi về pháp lý bất động sản…"
_PLACEHOLDER_STREAMING = "Đang nhận câu trả lời…"

pytestmark = [pytest.mark.e2e]


def _safe_local_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in _ALLOWED_HOSTS


def _fresh_tag(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _fresh_phone() -> str:
    """Valid Vietnamese mobile, unique per run; process-local only."""
    return f"090{int(uuid.uuid4().int % 10_000_000):07d}"


def _sales_credentials() -> tuple[str, str]:
    """Resolve process-only login aliases without exposing credential values."""
    email = os.environ.get("E2E_SALES_EMAIL") or os.environ.get("SALES_LOGIN_EMAIL")
    password = os.environ.get("E2E_SALES_PASSWORD") or os.environ.get("SALES_LOGIN_PASSWORD")
    return email or "", password or ""


def _project(page: Page, name: str) -> None:
    page.goto(f"{_BASE_URL}/login", wait_until="domcontentloaded")
    expect(page.get_by_text("Đăng nhập hệ thống")).to_be_visible(timeout=15_000)
    page.get_by_role("button", name="Trải nghiệm chatbot trước khi đăng ký").click()
    expect(page.get_by_role("listbox", name="Danh sách dự án")).to_be_visible(timeout=30_000)
    page.get_by_role("option", name=re.compile(name, re.I)).click()
    expect(page).to_have_url(re.compile(r"/project/[^?]+\?sessionId="), timeout=20_000)


def _capture_failures(page: Page, diagnostics: list[str]) -> None:
    def response(response) -> None:
        parsed = urlparse(response.url)
        if response.status >= 500 and parsed.hostname in _ALLOWED_HOSTS:
            # Status + path only; never response bodies.
            diagnostics.append(f"HTTP {response.status} {parsed.path}")

    page.on("pageerror", lambda error: diagnostics.append(f"pageerror {type(error).__name__}"))
    page.on("response", response)


def _state_snapshot(page: Page, diagnostics: list[str]) -> str:
    """PII-free bounded snapshot used when chat readiness cannot be proven."""
    lines = ["chat-ready could not be established"]
    try:
        lines.append(f"url={page.url}")
        lines.append(f"composer_enabled={page.get_by_label('Câu hỏi').is_enabled()}")
    except Exception:  # noqa: BLE001 — snapshot must not obscure the failure
        lines.append("url=<unreadable>")
    try:
        lines.append(f"wall_visible={page.get_by_role('button', name=_WALL_CTA_RE).is_visible()}")
    except Exception:  # noqa: BLE001
        lines.append("wall_visible=<unreadable>")
    lines.append(f"cta_visible={page.get_by_role('button', name=_CTA_RE).is_visible()}")
    if diagnostics:
        lines.append("diagnostics: " + "; ".join(diagnostics[:8]))
    return "\n".join(lines)


def _wait_chat_ready(page: Page, diagnostics: list[str]) -> str:
    """Wait for a genuine user-visible chat state, bounded.

    Returns "composer" when the Q&A composer is enabled (chat ready), or "wall"
    when the server-side quota wall alert is displayed. Both are the only
    legitimate states before submitting a lead; anything else is a failure with
    the diagnostics snapshot (no sleeps, no force/JS bypass).
    """
    composer = page.get_by_label("Câu hỏi")
    try:
        expect(composer).to_be_enabled(timeout=_CHAT_READY_TIMEOUT_MS)
        return "composer"
    except AssertionError:
        try:
            expect(page.get_by_role("button", name=_WALL_CTA_RE)).to_be_visible(timeout=5_000)
            return "wall"
        except AssertionError:
            raise AssertionError(_state_snapshot(page, diagnostics)) from None


def _wait_stream_finished(page: Page, diagnostics: list[str]) -> None:
    """Wait for the active stream to reach done via the composer placeholder.

    The send button is NOT a completion marker: Composer.tsx disables it on
    send (``setInput("")`` clears the draft and ``canSend`` requires non-empty
    input), so it can never return to enabled without a fresh draft. The only
    stable user-visible signal that streaming ended is the placeholder
    flipping from the streaming copy back to the idle copy (set on the same
    state that clears streaming).
    """
    composer = page.get_by_label("Câu hỏi")
    # Phase 1: require observing the streaming placeholder after the click.
    # Without this gate, a click that never dispatched can pass immediately on
    # the pre-existing idle placeholder.
    try:
        expect(composer).to_have_attribute(
            "placeholder", _PLACEHOLDER_STREAMING, timeout=_CHAT_READY_TIMEOUT_MS
        )
    except AssertionError:
        raise AssertionError(
            "stream did not start: streaming placeholder was not observed\n"
            + _state_snapshot(page, diagnostics)
        ) from None

    # Phase 2 (authoritative): idle placeholder means streaming ended.
    try:
        expect(composer).to_have_attribute(
            "placeholder", _PLACEHOLDER_IDLE, timeout=_TURN_FINISHED_TIMEOUT_MS
        )
    except AssertionError:
        raise AssertionError(
            "stream did not finish: idle placeholder was not observed\n"
            + _state_snapshot(page, diagnostics)
        ) from None


def _open_lead_form(page: Page, mode: str) -> None:
    """Click the visible entry point (CTA hint or wall CTA — never bypassed)."""
    if mode == "wall":
        page.get_by_role("button", name=_WALL_CTA_RE).click()
    else:
        page.get_by_role("button", name=_CTA_RE).click()
    expect(page.get_by_role("dialog")).to_be_visible(timeout=15_000)


def _reopen_entry_button(page: Page, mode: str) -> None:
    if mode == "wall":
        page.get_by_role("button", name=_WALL_CTA_RE).click()
    else:
        page.get_by_role("button", name=_CTA_RE).click()


def _dismiss_and_reopen_lead_form(page: Page, mode: str) -> None:
    """Prove the dismissal contract: close, reopen voluntarily, consent intact."""
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=15_000)
    dialog.get_by_role("button", name="Đóng", exact=True).click()
    expect(dialog).not_to_be_visible(timeout=15_000)
    _reopen_entry_button(page, mode)
    expect(dialog).to_be_visible(timeout=15_000)


async def _cleanup_tagged_lead(tag: str) -> int:
    """Delete only the test's tagged PG lead and its per-lead Firestore mirror."""
    settings = get_settings()
    conn = await asyncpg.connect(settings.pg_dsn, timeout=10)
    try:
        rows = await conn.fetch(
            "SELECT id FROM leads WHERE name = $1",
            f"Khách {tag}",
        )
        if len(rows) > 1:
            raise AssertionError("tagged cleanup matched more than one lead")
        if not rows:
            return 0
        lead_id = int(rows[0]["id"])
        await conn.execute("DELETE FROM sales_notification_reads WHERE lead_id = $1", lead_id)
        await conn.execute("DELETE FROM leads WHERE id = $1", lead_id)
    finally:
        await conn.close()
    mirror = await get_realtime_lead_mirror()
    try:
        await mirror.remove_lead_mirror(compute_lead_document_id(int(rows[0]["id"])))
    except Exception as exc:  # cleanup must expose an orphan rather than hide it
        raise AssertionError("tagged Firestore mirror cleanup failed") from exc
    return 1


def _run_cleanup(tag: str) -> int:
    """Run async cleanup on a private loop, including under pytest-asyncio."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, _cleanup_tagged_lead(tag)).result()


def _poll_until(condition, timeout: float, interval: float = 0.5) -> bool:
    """Bounded condition poll (the only allowed wait primitive for live APIs)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    return False


async def _verify_pg_preferred_assignment(tag: str, phone: str) -> int:
    """Prove PG assignment + consent. Raises PII-free AssertionErrors."""
    settings = get_settings()
    conn = await asyncpg.connect(settings.pg_dsn, timeout=10)
    try:
        rows = await conn.fetch(
            """
            SELECT id, status, assigned_sales_id, consent_service, consent_marketing, phone
            FROM leads WHERE name = $1 ORDER BY id DESC LIMIT 1
            """,
            f"Khách {tag}",
        )
    finally:
        await conn.close()
    if not rows:
        raise AssertionError("lead row was not persisted in PG")
    row = rows[0]
    if row["status"] != "assigned":
        raise AssertionError("lead status is not 'assigned'")
    if row["phone"] != phone:
        raise AssertionError("PG phone does not match the submitted phone")
    if row["consent_service"] is not True:
        raise AssertionError("service consent flag was not recorded")
    if row["consent_marketing"] is not False:
        raise AssertionError("marketing consent must not be inherited")
    preferred_uid = (settings.sales_preferred_firebase_uid or "").strip()
    if not preferred_uid:
        raise AssertionError("SALES_PREFERRED_FIREBASE_UID is not configured")
    sales_rows = await _fetch_sales_row(preferred_uid)
    if sales_rows is None:
        raise AssertionError("preferred sales firebase uid is not mapped to a sales row")
    if not sales_rows["is_active"]:
        raise AssertionError("preferred sales account is inactive")
    if row["assigned_sales_id"] != sales_rows["id"]:
        raise AssertionError("lead was not assigned to the preferred sales account")
    return int(row["id"])


async def _fetch_sales_row(firebase_uid: str):
    settings = get_settings()
    conn = await asyncpg.connect(settings.pg_dsn, timeout=10)
    try:
        rows = await conn.fetch(
            "SELECT id, firebase_uid, is_active FROM sales WHERE firebase_uid = $1",
            firebase_uid,
        )
    finally:
        await conn.close()
    return dict(rows[0]) if rows else None


async def _read_firestore_lead_document(lead_id: int) -> dict | None:
    """Read back the per-lead mirror document via Firestore REST (token in-process)."""
    settings = get_settings()
    from api.infrastructure.adapters.firestore_rest_mirror import (
        FirestoreRestLeadMirror,
    )
    from api.infrastructure.adapters.firestore_rest_mirror import (
        get_client as get_rest_client,
    )

    mirror = FirestoreRestLeadMirror(
        project_id=settings.firebase_project_id,
        service_account_client_email=settings.firebase_service_account_client_email,
        service_account_private_key=settings.firebase_service_account_private_key,
        rest_base_url=settings.firebase_firestore_rest_base_url,
    )
    client = await get_rest_client()
    token = await mirror._access_token()  # noqa: SLF001 — probe_firestore precedent
    doc_url = (
        f"{settings.firebase_firestore_rest_base_url}/projects/{settings.firebase_project_id}"
        f"/databases/(default)/documents/leads/{compute_lead_document_id(lead_id)}"
    )
    response = await client.get(doc_url, headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 404:
        return None
    return response.json().get("fields")


async def _verify_firestore_mirror(lead_id: int, tag: str, phone: str, expected_uid: str) -> None:
    """Prove display_name + masked phone; no raw phone anywhere in the document."""
    fields = await _read_firestore_lead_document(lead_id)
    if fields is None:
        raise AssertionError("Firestore lead mirror document was not found")
    if fields.get("display_name", {}).get("stringValue") != f"Khách {tag}":
        raise AssertionError("mirror display_name does not match the submitted name")
    if fields.get("masked_phone", {}).get("stringValue") != mask_phone(phone):
        raise AssertionError("mirror masked_phone does not match the expected mask")
    if fields.get("assigned_sales_firebase_uid", {}).get("stringValue") != expected_uid:
        raise AssertionError("mirror assigned sales firebase uid does not match")
    if fields.get("consent_service", {}).get("booleanValue") is not True:
        raise AssertionError("mirror consent_service is not true")
    if fields.get("consent_marketing", {}).get("booleanValue") is not False:
        raise AssertionError("mirror consent_marketing must be false")
    serialized = json.dumps(fields)
    if phone in serialized or "phone" in fields:
        raise AssertionError("raw phone leaked into the mirror document")


async def _sales_id_token(email: str, password: str) -> str:
    """Sign in against Firebase Auth for the sales API check. Never logged."""
    settings = get_settings()
    response = await httpx.AsyncClient(timeout=15.0).post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
        params={"key": settings.firebase_web_api_key},
        json={"email": email, "password": password, "returnSecureToken": True},
    )
    if response.status_code != 200:
        raise AssertionError("sales Firebase sign-in failed for the API check")
    token = response.json().get("idToken")
    if not token:
        raise AssertionError("sales Firebase sign-in returned no token")
    return token


async def _sales_api_exposes_lead(token: str, lead_id: str) -> bool:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{_BASE_URL}/api/sales/leads",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code != 200:
        return False
    return any(str(item.get("lead_id")) == lead_id for item in response.json().get("leads", []))


def _run(coro: object) -> object:
    """Run an async helper on a private loop from the sync test thread."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


@pytest.fixture()
def tagged_cleanup():
    """Register tags and remove only records owned by this test at teardown.

    A tag may be registered more than once along a happy path; the teardown
    dedupes so each tagged row is deleted exactly once (double-deleting a
    single row would assert on a second zero-row delete and mask the real
    teardown success).
    """
    tags: list[str] = []
    yield tags.append
    for tag in dict.fromkeys(tags):
        removed = _run_cleanup(tag)
        assert removed == 1, "tagged lead was not removed exactly once"


@pytest.fixture()
def browser_pair():
    """Two isolated browser contexts (customer + sales), PII-free yield."""
    if not _safe_local_url(_BASE_URL):
        pytest.skip("E2E_BASE_URL must be an http(s) localhost URL")
    email, password = _sales_credentials()
    if not email or not password:
        pytest.skip("E2E_SALES_EMAIL/E2E_SALES_PASSWORD not set")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        customer = browser.new_context()
        sales = browser.new_context()
        try:
            probe = customer.request.get(_BASE_URL, timeout=8_000)
            if not probe.ok:
                pytest.skip("FE is not reachable")
            # Credentials never enter the fixture value: pytest failure
            # representations would embed them in CI logs.
            yield browser, customer, sales
        finally:
            customer.close()
            sales.close()
            browser.close()


def _sales_ui_login(sales_page: Page, email: str, password: str) -> None:
    sales_page.goto(f"{_BASE_URL}/login", wait_until="domcontentloaded")
    email_field = sales_page.get_by_label("Email")
    password_field = sales_page.get_by_label("Mật khẩu")
    email_field.fill(email)
    password_field.fill(password)
    expect(email_field).to_have_value(email)
    expect(password_field).to_have_value(password)
    sales_page.get_by_role("button", name="Đăng nhập").click()
    # Role redirect contract: sales lands on the realtime lead board.
    expect(sales_page).to_have_url(
        re.compile(r"/sales/leads(?:$|\?)"), timeout=_LOGIN_REDIRECT_TIMEOUT_MS
    )


def test_customer_lead_reaches_sales_realtime(browser_pair, tagged_cleanup) -> None:
    """Full UI loop: chat-ready composer -> dismiss/reopen + consent -> one 201."""
    _browser, customer, sales = browser_pair
    email, password = _sales_credentials()
    tag = _fresh_tag("qa")
    phone = _fresh_phone()
    customer_page = customer.new_page()
    sales_page = sales.new_page()
    sales_diagnostics: list[str] = []
    _capture_failures(sales_page, sales_diagnostics)
    _sales_ui_login(sales_page, email, password)
    expect(sales_page.get_by_text("Danh sách lead")).to_be_visible(timeout=20_000)
    # Establish the persistent sales-shell listener before the customer creates
    # a lead; the following notification must be a post-snapshot arrival.
    sales_page.goto(f"{_BASE_URL}/sales/chat", wait_until="domcontentloaded")
    diagnostics: list[str] = []
    lead_post_count = 0

    def count_lead_post(response) -> None:
        nonlocal lead_post_count
        if response.request.method == "POST" and response.url.endswith("/api/lead"):
            lead_post_count += 1

    customer_page.on("response", count_lead_post)
    _capture_failures(customer_page, diagnostics)
    _project(customer_page, "Soleil")

    # Genuine chat-ready wait: enabled composer, or the server quota wall —
    # never a hidden/forced state, no sleeps, no JS bypass.
    mode = _wait_chat_ready(customer_page, diagnostics)
    if mode == "composer":
        cta = customer_page.get_by_role("button", name=_CTA_RE)
        composer = customer_page.get_by_label("Câu hỏi")
        send_button = customer_page.get_by_role("button", name="Gửi")
        for _turn in range(3):
            composer.fill(f"Giá căn hộ {tag}?")
            expect(send_button).to_be_enabled(timeout=_CHAT_READY_TIMEOUT_MS)
            send_button.click()
            # Stream-finish wait FIRST (placeholder back to idle): the CTA/
            # leadership hint is only computed once the turn is persisted
            # server-side, so it can never decide before the answer is done.
            _wait_stream_finished(customer_page, diagnostics)
            if customer_page.get_by_role("button", name=_WALL_CTA_RE).is_visible():
                mode = "wall"
                break
            try:
                expect(cta).to_be_visible(timeout=_ENTRY_TIMEOUT_MS)
                break
            except AssertionError:
                # The CTA hint fires from the 2nd useful turn onward for the
                # in-memory funnel and the 3rd persisted turn for the durable
                # path — a missing chip on earlier turns is expected, keep
                # the loop bounded by range(3).
                pass
        else:
            if not cta.is_visible():
                raise AssertionError(_state_snapshot(customer_page, diagnostics))
    # If the embedding/LLM path is broken (e.g. 403), the CTA never appears and
    # the wall alert path is the only legitimate state; otherwise the loss is
    # retained as a failure with the diagnostics snapshot (no UI flip-flop).
    if mode == "wall" and not customer_page.get_by_role("button", name=_WALL_CTA_RE).is_visible():
        raise AssertionError("quota wall visible but its lead button is missing")

    # Modal contract: open -> dismiss -> reopen (still consentable).
    _open_lead_form(customer_page, mode)
    _dismiss_and_reopen_lead_form(customer_page, mode)

    with customer_page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/api/lead")
            and response.status == 201
        ),
        timeout=_ENTRY_TIMEOUT_MS,
    ) as lead_response:
        dialog = customer_page.get_by_role("dialog")
        dialog.get_by_label("Tên anh/chị").fill(f"Khách {tag}")
        dialog.get_by_label("Số điện thoại").fill(phone)
        dialog.get_by_role("checkbox", name=_CONSENT_RE).check()
        dialog.get_by_role("button", name="Nhận tư vấn miễn phí").click()
    if lead_response.value.status != 201:
        raise AssertionError("lead submission was not accepted with 201")
    if lead_post_count != 1:
        raise AssertionError("lead POST fired more than once")
    tagged_cleanup(tag)
    expect(customer_page.get_by_text("Đã ghi nhận.")).to_be_visible(timeout=15_000)
    if any(item.startswith("HTTP 5") for item in diagnostics):
        raise AssertionError("customer flow surfaced HTTP 5xx responses")

    # PG source of truth: preferred sales assignment + consent split.
    preferred_uid = (get_settings().sales_preferred_firebase_uid or "").strip()
    lead_id = _run(_verify_pg_preferred_assignment(tag, phone))
    _run(_verify_firestore_mirror(lead_id, tag, phone, preferred_uid))

    # Sales API visibility (bearer token in-process, never printed).
    token = _run(_sales_id_token(email, password))
    if not _poll_until(lambda: _run(_sales_api_exposes_lead(token, str(lead_id))), 30.0):
        raise AssertionError("sales API did not expose the new lead within 30s")

    # The chat shell was already live before the customer submitted the lead:
    # prove its post-snapshot notification, unread badge, and click navigation.
    expect(sales_page.get_by_text("Lead mới", exact=True)).to_be_visible(
        timeout=_SALES_VISIBILITY_TIMEOUT_MS
    )
    notification_button = sales_page.get_by_role("button", name=re.compile("Thông báo"))
    expect(notification_button).to_have_attribute(
        "aria-label", re.compile("chưa đọc"), timeout=_SALES_VISIBILITY_TIMEOUT_MS
    )
    notification_button.click()
    notification_lead = sales_page.get_by_role(
        "link", name=re.compile(rf"^Khách {re.escape(tag)}\s+soleil$", re.I)
    )
    expect(notification_lead).to_be_visible(timeout=10_000)
    notification_lead.click()
    expect(sales_page).to_have_url(re.compile(r"/sales/leads\?lead="), timeout=10_000)
    lead_row = sales_page.get_by_role("row", name=re.compile(rf"Khách {re.escape(tag)}\s+\d+\*+\d+\s+soleil", re.I))
    expect(lead_row).to_be_visible(timeout=_SALES_VISIBILITY_TIMEOUT_MS)

    # Lead detail must reveal the full phone with explicit copy/tel affordances,
    # while the list remains masked and the linked chat transcript is visible.
    sales_page.keyboard.press("Escape")
    lead_row.get_by_role("button", name="Chi tiết").click()
    detail = sales_page.get_by_role("dialog")
    expect(detail).to_be_visible(timeout=15_000)
    expect(detail.get_by_text(mask_phone(phone), exact=True)).to_be_visible()
    expect(detail.get_by_text(f"Giá căn hộ {tag}?", exact=True).first).to_be_visible(timeout=15_000)
    # The eye-icon reveal control resolves its customer identity asynchronously
    # (the realtime snapshot / REST row carries the opaque customer_id), so wait
    # for it to become ENABLED before clicking. The prior "stuck disabled"
    # timeout was this async identity-resolution gap, not a product defect.
    reveal_button = detail.get_by_role("button", name="Hiện số đầy đủ")
    expect(reveal_button).to_be_enabled(timeout=15_000)
    reveal_button.click()
    expect(detail.get_by_text(phone, exact=True)).to_be_visible(timeout=15_000)
    expect(detail.get_by_role("link", name=re.compile(r"Gọi|tel:", re.I))).to_be_visible()
    # Reveal state is conveyed by the eye icon alone: the purple "Đã hiển thị
    # đầy đủ" pin is gone, and copy is a compact icon button.
    expect(detail.get_by_text("Đã hiển thị đầy đủ")).to_have_count(0)
    expect(detail.get_by_role("button", name="Sao chép số điện thoại")).to_be_visible()

    # Training shares the application header, supports project switching, and
    # excludes customer-only history, map, CTA, and quota surfaces.
    sales_page.goto(f"{_BASE_URL}/sales/train", wait_until="domcontentloaded")
    expect(sales_page.get_by_text("đây là chế độ đào tạo nội bộ", exact=False).first).to_be_visible(
        timeout=20_000
    )
    expect(sales_page.get_by_role("combobox", name="Dự án đào tạo")).to_be_visible(timeout=15_000)
    expect(sales_page.get_by_label("Lịch sử chat")).not_to_be_visible()
    expect(sales_page.get_by_role("button", name="Gọi tư vấn")).not_to_be_visible()
    expect(sales_page.get_by_text(re.compile("lượt tư vấn|quota", re.I))).not_to_be_visible()
    sales_page.goto(f"{_BASE_URL}/train", wait_until="domcontentloaded")
    expect(sales_page).to_have_url(re.compile(r"/sales/train(?:$|\?)"), timeout=15_000)
    if any(item.startswith("HTTP 5") for item in sales_diagnostics):
        raise AssertionError("sales flow surfaced HTTP 5xx responses")

    customer_page.close()
    sales_page.close()


def test_lead_pipeline_no_llm_direct_acceptance(browser_pair, tagged_cleanup) -> None:
    """Corroboration without any LLM: direct lead POST -> PG -> mirror -> sales.

    This is the acceptance path to run when the CTA could not be reached in the
    browser flow (e.g. embedding service 403): the lead pipeline, realtime
    mirror, preferred assignment and sales visibility must still hold.
    """
    _browser, customer, sales = browser_pair
    email, password = _sales_credentials()
    tag = _fresh_tag("qa-direct")
    phone = _fresh_phone()
    settings = get_settings()

    response = httpx.post(
        f"{_BASE_URL}/api/lead",
        json={
            "project_key": "soleil",
            "name": f"Khách {tag}",
            "phone": phone,
            "consent": True,
        },
        timeout=20.0,
    )
    if response.status_code != 201:
        raise AssertionError(f"direct lead POST was not accepted (HTTP {response.status_code})")
    lead_id = int(response.json()["lead_id"])
    tagged_cleanup(tag)

    preferred_uid = (settings.sales_preferred_firebase_uid or "").strip()
    lead_id = _run(_verify_pg_preferred_assignment(tag, phone))
    _run(_verify_firestore_mirror(lead_id, tag, phone, preferred_uid))

    token = _run(_sales_id_token(email, password))
    if not _poll_until(lambda: _run(_sales_api_exposes_lead(token, str(lead_id))), 30.0):
        raise AssertionError("sales API did not expose the lead within 30s")

    sales_page = sales.new_page()
    sales_diagnostics: list[str] = []
    _capture_failures(sales_page, sales_diagnostics)
    _sales_ui_login(sales_page, email, password)
    expect(sales_page.get_by_text("Danh sách lead")).to_be_visible(timeout=20_000)
    expect(sales_page.get_by_text(f"Khách {tag}", exact=True)).to_be_visible(
        timeout=_SALES_VISIBILITY_TIMEOUT_MS
    )
    sales_page.goto(f"{_BASE_URL}/sales/train", wait_until="domcontentloaded")
    expect(sales_page.get_by_text("đây là chế độ đào tạo nội bộ", exact=False).first).to_be_visible(
        timeout=20_000
    )
    expect(sales_page.get_by_text("403", exact=True)).not_to_be_visible(timeout=10_000)
    if any(item.startswith("HTTP 5") for item in sales_diagnostics):
        raise AssertionError("sales flow surfaced HTTP 5xx responses")
    sales_page.close()
