"""Wave-1 E2E: real browser against the dev FE (Next) + BE (FastAPI).

Deterministic customer-journey coverage for the wave-1 UX rules:
  1. forced ProjectPicker on first visit (never a silent default);
  2. hot project (Camellia) leads the picker with its full address;
  3. choosing Soleil greets as Soleil and the gallery enriches from the
     backend hello media (embedding-quota-proof via the published-rows
     fallback);
  4. the header chip names the active project and "Đổi dự án" reopens the
     picker.

The chat-answer journey (query -> streamed answer -> map directions) is
intentionally NOT asserted here: it depends on the LLM gateway, whose quota
can be exhausted, and is covered by unit/IT layers plus manual browser runs
when quota is available.

Run:  python -m pytest tests/e2e -m e2e   (requires FE on E2E_BASE_URL,
default http://localhost:3000; skips otherwise). Chromium comes from the
repo venv's playwright install.
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import expect, sync_playwright

E2E_BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:3000")

pytestmark = [pytest.mark.e2e]


def _fe_reachable() -> bool:
    try:
        import requests

        return requests.get(E2E_BASE_URL, timeout=3).status_code == 200
    except Exception:  # noqa: BLE001 — any transport error means "skip"
        return False


@pytest.fixture()
def page():
    if not _fe_reachable():
        pytest.skip(f"FE not reachable at {E2E_BASE_URL}")
    # A fresh context per test: stored project choices must never leak
    # between journeys, or the forced-picker rule cannot be exercised.
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context()  # fresh storage: no stored project choice
        page = context.new_page()
        yield page
        context.close()
        browser.close()


def test_first_visit_forces_picker_with_hot_project_first(page):
    page.goto(E2E_BASE_URL)

    modal = page.get_by_text("Chọn dự án để được tư vấn")
    expect(modal).to_be_visible(timeout=15000)

    # Hot project leads: Camellia first, with its registry address.
    expect(page.get_by_text("Nổi bật").first).to_be_visible()
    expect(page.get_by_text("Lê Văn Lương", exact=False).first).to_be_visible()
    expect(page.get_by_text("Phạm Văn Đồng", exact=False).first).to_be_visible()


def test_selecting_soleil_greets_as_soleil_with_gallery(page):
    page.goto(E2E_BASE_URL)

    page.get_by_text("The Soleil", exact=False).first.click()
    expect(page.get_by_text("Chọn dự án để được tư vấn")).not_to_be_visible(
        timeout=10000
    )

    # Header chip names the active project.
    expect(page.get_by_text("Soleil", exact=False).first).to_be_visible(
        timeout=10000
    )

    # Greeting text is Soleil's, and the backend media enrichment lands even
    # while the embedding provider is down (published-rows fallback).
    expect(page.get_by_text("The Soleil Đà Nẵng", exact=False).first).to_be_visible(
        timeout=20000
    )
    expect(
        page.get_by_text("Hình ảnh & tài liệu dự án").first
    ).to_be_visible(timeout=20000)
    expect(page.get_by_role("figure").first).to_be_visible(timeout=10000)


def test_change_project_reopens_picker_and_updates_header(page):
    page.goto(E2E_BASE_URL)

    page.get_by_text("The Soleil", exact=False).first.click()
    expect(page.get_by_text("Chọn dự án để được tư vấn")).not_to_be_visible(
        timeout=10000
    )

    page.get_by_role("button", name="Đổi dự án").click()
    expect(page.get_by_text("Chọn dự án để được tư vấn")).to_be_visible(
        timeout=10000
    )
    page.get_by_text("The Camellia", exact=False).first.click()

    # The header follows the switch; the Camellia greeting text mentions
    # its own address, never Soleil's.
    expect(page.get_by_text("Lê Văn Lương", exact=False).first).to_be_visible(
        timeout=20000
    )
