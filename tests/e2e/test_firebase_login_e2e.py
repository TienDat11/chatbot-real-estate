"""Firebase login E2E: real browser + real Firebase Auth (story 8.3 setup gate).

Covers the full manual-setup contract on the FE side:
  1. a real admin account can sign in from /login and lands on /admin;
  2. the same admin (role=admin) can open /crm (admin|sales allowed);
  3. /train stays sales-only — the admin gets the 403 result, not the workspace.

Credentials are NEVER hard-coded: the test reads E2E_LOGIN_EMAIL and
E2E_LOGIN_PASSWORD from the environment and skips when they are absent, so no
usable credential literal ever lands in source (secrets policy).

Only http/https with an explicit host is honoured for E2E_BASE_URL; anything
else skips. Reachability is probed with the browser's own navigation (no
server-side HTTP call from this file).

Run:  E2E_LOGIN_EMAIL=... E2E_LOGIN_PASSWORD=... python -m pytest tests/e2e/test_firebase_login_e2e.py -m e2e
      (requires FE on E2E_BASE_URL, default http://localhost:3000; skips otherwise)
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

E2E_BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:3000")
LOGIN_EMAIL = os.environ.get("E2E_LOGIN_EMAIL", "")
LOGIN_PASSWORD = os.environ.get("E2E_LOGIN_PASSWORD", "")

pytestmark = [pytest.mark.e2e]


def _base_url_is_safe() -> bool:
    parsed = urlparse(E2E_BASE_URL)
    # Allow only explicit http(s) targets with a host — blocks scheme-less,
    # file:, and other unexpected inputs from leaking into navigation.
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


@pytest.fixture()
def authed_page():
    if not _base_url_is_safe():
        pytest.skip(f"E2E_BASE_URL must be http(s) with a host, got {E2E_BASE_URL!r}")
    if not LOGIN_EMAIL or not LOGIN_PASSWORD:
        pytest.skip("E2E_LOGIN_EMAIL / E2E_LOGIN_PASSWORD not set in environment")
    # Fresh context per run: persisted auth state must never leak between runs.
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        try:
            # Probe with the browser's own navigation; a transport failure
            # means the FE is down, which is a skip, not a failure.
            page.goto(
                f"{E2E_BASE_URL}/login",
                wait_until="domcontentloaded",
                timeout=8000,
            )
        except Exception:  # noqa: BLE001 — FE unreachable means "skip"
            pytest.skip(f"FE not reachable at {E2E_BASE_URL}")
        yield page
        context.close()
        browser.close()


def _sign_in(page) -> None:
    page.goto(f"{E2E_BASE_URL}/login")
    page.get_by_label("Email").fill(LOGIN_EMAIL)
    page.get_by_label("Mật khẩu").fill(LOGIN_PASSWORD)
    page.get_by_role("button", name="Đăng nhập").click()
    # LoginScreen redirects to /admin for role=admin once `user` lands.
    expect(page).to_have_url(f"{E2E_BASE_URL}/admin", timeout=20000)


def test_admin_login_lands_on_admin(authed_page):
    _sign_in(authed_page)

    # RequireRole(allowedRoles=["admin"]) let the workspace render (not 403).
    expect(authed_page.get_by_text("403").first).not_to_be_visible()


def test_admin_can_open_crm(authed_page):
    _sign_in(authed_page)

    authed_page.goto(f"{E2E_BASE_URL}/crm")
    # admin is allowed on /crm: the guard renders the workspace, never 403.
    expect(authed_page.get_by_text("403").first).not_to_be_visible(timeout=15000)
    expect(authed_page.get_by_text("Đang tải thông tin đăng nhập")).not_to_be_visible(
        timeout=15000
    )


def test_admin_is_denied_on_train(authed_page):
    _sign_in(authed_page)

    authed_page.goto(f"{E2E_BASE_URL}/train")
    # /train is sales-only: the admin must see the denial result.
    expect(authed_page.get_by_text("403").first).to_be_visible(timeout=15000)
    expect(
        authed_page.get_by_text(
            "Bạn không có quyền truy cập trang này.", exact=False
        ).first
    ).to_be_visible(timeout=10000)
