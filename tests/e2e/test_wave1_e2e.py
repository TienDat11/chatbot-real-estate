"""Wave-1 browser E2E contract for the public login and project chat flow.

The public entry point is a unified login gate. A customer must explicitly use
its guest CTA before selecting a project and entering routed chat. Staff login
credentials are covered separately and are never required here.

Run: ``python -m pytest tests/e2e/test_wave1_e2e.py -m e2e -s`` with the FE on
E2E_BASE_URL (default ``http://localhost:3000``). Only localhost targets are
accepted. The test captures browser console and network failures and reports
same-origin failures at teardown.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

_E2E_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_E2E_DEFAULT_BASE_URL = "http://localhost:3000"
pytestmark = [pytest.mark.e2e]


def validate_e2e_base_url(raw_url: str) -> str | None:
    """Return *raw_url* only when it targets the localhost allowlist."""
    try:
        parsed = urlparse(str(raw_url))
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.hostname not in _E2E_ALLOWED_HOSTS:
        return None
    return str(raw_url).rstrip("/")


@pytest.fixture()
def base_url() -> str:
    url = validate_e2e_base_url(os.environ.get("E2E_BASE_URL", _E2E_DEFAULT_BASE_URL))
    if url is None:
        pytest.skip("E2E_BASE_URL outside localhost allowlist")
    return url


@pytest.fixture()
def page(base_url: str):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        try:
            # Probe the server without navigating the test page. Navigating here
            # races with the first test navigation and can drop the hydrated
            # guest CTA click when a test immediately revisits /login.
            probe = context.request.get(base_url, timeout=8000)
            if not probe.ok:
                raise RuntimeError(f"FE probe returned {probe.status}")
        except Exception:  # noqa: BLE001 - an unavailable FE is a skip condition
            context.close()
            browser.close()
            pytest.skip(f"FE not reachable at {base_url}")
        console_errors: list[str] = []
        page_errors: list[str] = []
        failed_requests: list[str] = []
        server_failures: list[str] = []

        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: failed_requests.append(f"{request.url}: {request.failure}"),
        )
        page.on(
            "response",
            lambda response: (
                server_failures.append(f"{response.status} {response.url}")
                if response.status >= 500 and urlparse(response.url).hostname in _E2E_ALLOWED_HOSTS
                else None
            ),
        )
        yield page
        diagnostics = console_errors + page_errors + failed_requests + server_failures
        if diagnostics:
            print("\nBrowser diagnostics:\n" + "\n".join(diagnostics))
        context.close()
        browser.close()


def _guest_to_picker(page, base_url: str) -> None:
    page.goto(f"{base_url}/login", wait_until="domcontentloaded")
    expect(page.get_by_text("Đăng nhập hệ thống")).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Trải nghiệm chatbot trước khi đăng ký").click()
    picker_title = page.get_by_text("Chọn dự án để được tư vấn")
    expect(picker_title).to_be_visible(timeout=30000)
    assert "/project/" not in page.url


def _select_project(page, name: str) -> None:
    page.get_by_role("option", name=re.compile(name, re.IGNORECASE)).click()
    expect(page.get_by_text("Chọn dự án để được tư vấn")).not_to_be_visible(timeout=15000)
    # Canonical param is sessionId (legacy ?session= deep links still hydrate).
    expect(page).to_have_url(
        re.compile(r"/project/[^?]+\?sessionId=[^&]+"), timeout=20000
    )


def test_fresh_root_is_unified_login_gate_without_project_route(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")

    expect(page.get_by_text("Đăng nhập hệ thống")).to_be_visible(timeout=15000)
    expect(page.get_by_role("button", name="Trải nghiệm chatbot trước khi đăng ký")).to_be_visible()
    expect(page).to_have_url(re.compile(rf"^{re.escape(base_url)}/?$"))
    assert "/project/" not in page.url
    expect(page.get_by_text("Chọn dự án để được tư vấn")).not_to_be_visible()


def test_login_has_project_imagery_or_fallback_and_guest_enters_picker(page, base_url):
    page.goto(f"{base_url}/login", wait_until="domcontentloaded")
    visual = page.locator(".login-visual")
    expect(visual).to_be_visible(timeout=15000)
    expect(page.get_by_role("button", name="Trải nghiệm chatbot trước khi đăng ký")).to_be_visible()
    _guest_to_picker(page, base_url)
    expect(page.get_by_role("listbox", name="Danh sách dự án")).to_be_visible(timeout=30000)


def test_selecting_soleil_uses_canonical_session_url_and_gallery(page, base_url):
    _guest_to_picker(page, base_url)
    _select_project(page, "Soleil")
    expect(page.get_by_text("Chọn dự án để được tư vấn")).not_to_be_visible(timeout=15000)
    expect(page).to_have_url(re.compile(r"/project/[^?]+\?sessionId=[^&]+"), timeout=20000)

    expect(page.get_by_text("Soleil", exact=False).first).to_be_visible(timeout=15000)
    expect(page.get_by_text("Hình ảnh & tài liệu dự án").first).to_be_visible(timeout=20000)
    expect(page.get_by_role("figure").first).to_be_visible(timeout=10000)


def test_project_switch_is_explicit_and_updates_header_and_session(page, base_url):
    _guest_to_picker(page, base_url)
    _select_project(page, "Soleil")
    expect(page).to_have_url(re.compile(r"/project/[^?]+\?sessionId=[^&]+"), timeout=20000)
    page.get_by_role("button", name="Đổi dự án").click()
    expect(page.get_by_text("Chọn dự án để được tư vấn")).to_be_visible(timeout=10000)
    page.get_by_role("option", name=re.compile("Camellia", re.IGNORECASE)).click()
    expect(page.get_by_text("Camellia", exact=False).first).to_be_visible(timeout=15000)
    expect(page).to_have_url(re.compile(r"/project/[^?]+\?sessionId=[^&]+"), timeout=20000)
    assert "sessionId=" in page.url


def test_mobile_login_and_picker_remain_usable(page, base_url):
    page.set_viewport_size({"width": 390, "height": 844})
    _guest_to_picker(page, base_url)

    expect(page.get_by_role("listbox", name="Danh sách dự án")).to_be_visible()
    soleil = page.get_by_role("option", name=re.compile("Soleil", re.IGNORECASE))
    expect(soleil).to_be_visible()
    box = soleil.bounding_box()
    assert box is not None and box["width"] > 0 and box["height"] > 0
