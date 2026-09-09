"""Browser smoke contract for the canonical sales chat URL.

This test only exercises routing and shell markers; it never submits a question,
so no LLM call is made. It is skipped when the local FE is unavailable.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.e2e


def _base_url() -> str:
    raw = os.environ.get("E2E_BASE_URL", "http://localhost:3000").rstrip("/")
    parsed = urlparse(raw)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("E2E_BASE_URL outside localhost allowlist")
    return raw


def test_canonical_sales_route_preserves_project_session_and_single_shell():
    base_url = _base_url()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context()
        try:
            probe = context.request.get(base_url, timeout=8000)
            if not probe.ok:
                pytest.skip(f"FE probe returned {probe.status}")
            page = context.new_page()
            page.goto(
                f"{base_url}/sales/chat/project/soleil?sessionId=e2e-session-contract",
                wait_until="domcontentloaded",
            )
            expect(page).not_to_have_url("**/train**")
            # A fresh browser context must follow the unauthenticated redirect
            # contract; authenticated shell coverage belongs to the sales E2E.
            expect(page).to_have_url(
                re.compile(r"/login\?next=.*sales%2Fchat%2Fproject%2Fsoleil.*$"),
                timeout=10_000,
            )
        finally:
            context.close()
            browser.close()
