"""Integration tests for lead submission dedup against a REAL Postgres (QA D3).

Reproduces the QA scenario that exposed the defect — 10 identical rapid
POST /api/lead — through the production route + adapter stack (real
asyncpg pool, real ``leads`` table). The acceptance is the hard criterion
from the QA report: exactly one row persisted, nine 409 responses, plus the
10-minute dedup window honoured at its boundary.

The whole module skips cleanly when the live DB is unavailable so the gate
never blocks on a missing environment. Every test cleans up the rows it
created (leads + their assignment logs) in a finally block.

Why each test closes the lead pool: ``postgres_leads`` keeps a module-global
asyncpg pool bound to the event loop that created it. Each test drives its
coroutine with its own ``asyncio.run()`` loop, so a pool left open would
raise "Event loop is closed" for the next real-DB test (same reason
``sql_leg`` pools are closed per-run in test_integration_image_search.py).
"""

from __future__ import annotations

import asyncio
import time

import asyncpg
import httpx
import pytest

from api.application.services.sql_leg import build_dsn
from api.infrastructure.adapters.postgres_leads import close_lead_pool
from api.infrastructure.config.config import settings
from api.interfaces.api.main import create_app

# --- module-level skip gate --------------------------------------------------

_MISSING_DB_REASON = "POSTGRES_PASSWORD is not set; skipping real-DB integration tests"


async def _probe_db() -> None:
    """Open one asyncpg connection and run SELECT 1 (nothing more)."""
    conn = await asyncio.wait_for(
        asyncpg.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            database=settings.postgres_database,
        ),
        timeout=5,
    )
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


def _skip_reason() -> str | None:
    if not settings.postgres_password:
        return _MISSING_DB_REASON
    return None


_SKIP_REASON = _skip_reason()
if _SKIP_REASON is None:
    try:
        asyncio.run(_probe_db())
    except Exception as exc:  # noqa: BLE001 - any probe failure means skip
        _SKIP_REASON = f"DB probe failed ({type(exc).__name__}: {exc})"
if _SKIP_REASON:
    pytest.skip(_SKIP_REASON, allow_module_level=True)

# --- helpers -----------------------------------------------------------------

# Hard ceiling so a hung request fails the test instead of blocking the suite.
_SUBMIT_TIMEOUT_S = 60.0

# Guarantees uniqueness even when two phones are minted in the same millisecond.
_phone_counter = 0


def _unique_phone() -> str:
    """Valid VN mobile unique per run so reruns never collide on dedup."""
    global _phone_counter
    _phone_counter += 1
    digits = (time.time_ns() // 1_000_000 * 10 + _phone_counter) % 10_000_000
    return "090" + f"{digits:07d}"


def _payload(phone: str) -> dict:
    return {
        "project_key": "camellia",
        "session_id": f"dedup-it-{phone}",
        "device_id": f"dedup-it-device-{phone}",
        "name": "Dedup IT",
        "phone": phone,
        "consent": True,
    }


async def _cleanup_phone(phone: str) -> None:
    """Remove every trace of a test phone (assignment logs first, FK-safe)."""
    conn = await asyncpg.connect(build_dsn())
    try:
        await conn.execute(
            "DELETE FROM sales_assignment_log WHERE lead_id IN "
            "(SELECT id FROM leads WHERE phone = $1)",
            phone,
        )
        await conn.execute("DELETE FROM leads WHERE phone = $1", phone)
    finally:
        await conn.close()


async def _count_leads(phone: str) -> int:
    conn = await asyncpg.connect(build_dsn())
    try:
        return int(await conn.fetchval("SELECT count(*) FROM leads WHERE phone = $1", phone))
    finally:
        await conn.close()


async def _seed_lead(phone: str, minutes_old: int) -> None:
    """Insert a lead with a pinned created_at to test the window boundary."""
    conn = await asyncpg.connect(build_dsn())
    try:
        await conn.execute(
            """INSERT INTO leads
                 (session_id, project_key, device_id, name, phone, consent,
                  note, budget_vnd, created_at)
               VALUES ($1, 'camellia', NULL, 'Seeded', $2, true, NULL, NULL,
                       now() - ($3 * interval '1 minute'))""",
            f"dedup-it-seed-{phone}", phone, minutes_old,
        )
    finally:
        await conn.close()


# --- tests -------------------------------------------------------------------


def test_ten_concurrent_identical_submits_create_exactly_one_lead() -> None:
    async def scenario() -> None:
        phone = _unique_phone()
        try:
            app = create_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                responses = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            client.post("/api/lead", json=_payload(phone))
                            for _ in range(10)
                        )
                    ),
                    timeout=_SUBMIT_TIMEOUT_S,
                )
            statuses = sorted(r.status_code for r in responses)
            assert statuses == [201] + [409] * 9
            created = [r for r in responses if r.status_code == 201]
            assert len(created) == 1
            assert created[0].json()["lead_id"] > 0
            for rejected in (r for r in responses if r.status_code == 409):
                detail = rejected.json()["detail"]
                assert detail["code"] == "duplicate_lead"
                assert detail["lead_id"] == created[0].json()["lead_id"]
            assert await _count_leads(phone) == 1
        finally:
            await _cleanup_phone(phone)
            await close_lead_pool()

    asyncio.run(scenario())


def test_dedup_window_boundary_ten_minutes() -> None:
    async def scenario() -> None:
        stale_phone = _unique_phone()
        fresh_phone = _unique_phone()
        try:
            # 11 minutes old: outside the window -> a new submit must win.
            await _seed_lead(stale_phone, minutes_old=11)
            # 9 minutes old: inside the window -> a new submit must 409.
            await _seed_lead(fresh_phone, minutes_old=9)

            app = create_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                stale_response = await client.post(
                    "/api/lead", json=_payload(stale_phone)
                )
                fresh_response = await client.post(
                    "/api/lead", json=_payload(fresh_phone)
                )
            assert stale_response.status_code == 201
            assert await _count_leads(stale_phone) == 2
            assert fresh_response.status_code == 409
            assert fresh_response.json()["detail"]["code"] == "duplicate_lead"
            assert await _count_leads(fresh_phone) == 1
        finally:
            await _cleanup_phone(stale_phone)
            await _cleanup_phone(fresh_phone)
            await close_lead_pool()

    asyncio.run(scenario())
