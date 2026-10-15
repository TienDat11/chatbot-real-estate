"""Offline runtime gates and actual application lifespan; no provider or DB I/O."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.application.services import zalo_bot_dispatch as dispatch
from api.application.services.zalo_bot_reconciliation import ZaloRuntime
from api.infrastructure.adapters import zalo_bot_runtime as runtime
from api.infrastructure.config.config import Settings


def settings(**changes):
    values = dict(
        zalo_bot_enabled=True,
        zalo_bot_identity_secret="runtime-identity-independent-key-123456",
        zalo_bot_webhook_secret="runtime-webhook-secret",
        zalo_bot_token="runtime-provider-token",
        app_env="test",
        enforce_approved_llm_provider=False,
        cors_origins=["https://allowed.example"],
    )
    values.update(changes)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("field,value", [
    ("zalo_bot_burst_window_seconds", 301),
    ("zalo_bot_burst_max_events", 10001),
    ("zalo_bot_poll_seconds", 0),
    ("zalo_bot_batch_size", 101),
    ("zalo_bot_max_attempts", 101),
    ("zalo_bot_lease_seconds", 29),
    ("zalo_bot_core_timeout_seconds", 0),
    ("zalo_bot_lease_margin_seconds", 5),
    ("zalo_bot_retry_max_age_hours", 0),
    ("zalo_bot_terminal_retention_days", 0),
    ("zalo_bot_batch_size", True),
    ("zalo_bot_batch_size", 20.0),
    ("zalo_bot_batch_size", "20.0"),
    ("zalo_bot_batch_size", "２０"),
])
def test_runtime_rejects_unsafe_values_without_partial_normalization(field, value):
    cfg = settings(zalo_bot_poll_seconds="7", **{field: value}) if field != "zalo_bot_poll_seconds" else settings(**{field: value})
    before = cfg.zalo_bot_poll_seconds
    with pytest.raises(ValueError, match="ZALO_CONFIG_INVALID"):
        cfg.validate_zalo_runtime()
    assert cfg.zalo_bot_poll_seconds == before


def test_runtime_lease_coverage_rejects_equality_and_accepts_decimal_env(monkeypatch):
    cfg = settings(zalo_bot_lease_seconds=105)
    with pytest.raises(ValueError):
        cfg.validate_zalo_runtime()
    monkeypatch.setenv("ZALO_BOT_POLL_SECONDS", "7")
    cfg = settings(zalo_bot_lease_seconds="106")
    cfg.validate_zalo_runtime()
    assert cfg.zalo_bot_lease_seconds == 106
    assert cfg.zalo_bot_poll_seconds == 7


@pytest.mark.asyncio
async def test_disabled_start_and_builder_never_validate_or_build(monkeypatch):
    cfg = SimpleNamespace(zalo_bot_enabled=False)
    build = AsyncMock(side_effect=AssertionError("disabled build"))
    monkeypatch.setattr(runtime, "build_zalo_processor", build)
    old = AsyncMock()
    dispatch.set_scheduler(old)
    try:
        assert await runtime.start_zalo_runtime(cfg) is None
        await dispatch.schedule(1)
        old.assert_not_called()
        build.assert_not_called()
    finally:
        dispatch.reset_scheduler()


@pytest.mark.asyncio
async def test_start_schema_gate_installation_and_failure_are_safe(monkeypatch, caplog):
    from api.infrastructure.adapters import postgres_zalo_events

    order = []
    processor = SimpleNamespace(halted=False, process=AsyncMock(), stop=AsyncMock())
    store = SimpleNamespace(maintain=AsyncMock(return_value=(0, 0)), due_candidates=AsyncMock(return_value=[]))

    async def validate_schema():
        order.append("schema")

    store.validate_schema = validate_schema
    monkeypatch.setattr(postgres_zalo_events, "PostgresZaloEventStore", lambda **kwargs: store)

    def build(**kwargs):
        assert order == ["schema"]
        order.append("build")
        return processor

    monkeypatch.setattr(runtime, "build_zalo_processor", build)
    handle = await runtime.start_zalo_runtime(settings())
    try:
        assert handle is not None
        await dispatch.schedule(123)
        processor.process.assert_not_called()  # wake only; no inline execution
        assert handle.signal.is_set()
    finally:
        await runtime.stop_zalo_runtime(handle)
    processor.stop.assert_awaited_once()
    assert handle.task.done()
    store.validate_schema = AsyncMock(side_effect=RuntimeError("private-db-url-token"))
    assert await runtime.start_zalo_runtime(settings()) is None
    assert order == ["schema", "build"]
    assert "private-db-url-token" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_open_demo_access_emits_safe_warning(monkeypatch, caplog):
    from api.infrastructure.adapters import postgres_zalo_events
    processor = SimpleNamespace(halted=False, process=AsyncMock(), stop=AsyncMock())
    store = SimpleNamespace(
        maintain=AsyncMock(return_value=(0, 0)),
        due_candidates=AsyncMock(return_value=[]),
        validate_schema=AsyncMock(),
    )
    monkeypatch.setattr(postgres_zalo_events, "PostgresZaloEventStore", lambda **kwargs: store)
    monkeypatch.setattr(runtime, "build_zalo_processor", lambda **kwargs: processor)
    caplog.set_level(logging.WARNING)
    with caplog.at_level(logging.WARNING, logger="api.zalo_bot_runtime"):
        handle = await runtime.start_zalo_runtime(settings(zalo_bot_open_demo_access=True))
    try:
        assert handle is not None
        assert "ZALO_OPEN_DEMO_ACCESS_ENABLED" in caplog.text
        for secret in ("runtime-identity-independent-key-123456",
                       "zalotest", "127.0.0.1"):
            assert secret not in caplog.text
    finally:
        await runtime.stop_zalo_runtime(handle)
    assert handle.task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["wake-during-work", "missed-wake-poll"])
async def test_startup_and_later_sweeps_recover_durable_work(trigger):
    processed = []
    swept_twice = asyncio.Event()
    cfg = SimpleNamespace(zalo_bot_enabled=True, zalo_bot_batch_size=2, zalo_bot_poll_seconds=0.01)
    processor = SimpleNamespace(halted=False)
    store = SimpleNamespace(maintain=AsyncMock(return_value=(0, 0)))
    sweeps = 0

    async def due_candidates():
        nonlocal sweeps
        sweeps += 1
        if sweeps == 1:
            return [1, 2, 999]
        return [3]

    async def process(event_id):
        processed.append(event_id)
        if event_id == 1 and trigger == "wake-during-work":
            cfg.zalo_bot_poll_seconds = 3600
            await handle.wake(987)  # hint is not an event to process
        if event_id == 3:
            processor.halted = True
            swept_twice.set()

    store.due_candidates = due_candidates
    processor.process = process
    handle = ZaloRuntime(store=store, processor=processor, settings=cfg)
    task = asyncio.create_task(handle.run())
    try:
        await asyncio.wait_for(swept_twice.wait(), timeout=1)
        await task
        assert processed == [1, 2, 3]
        assert store.maintain.await_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_during_maintenance_prevents_claims_and_resets_first():
    entered = asyncio.Event()
    resume = asyncio.Event()
    cfg = SimpleNamespace(zalo_bot_enabled=True, zalo_bot_batch_size=20, zalo_bot_poll_seconds=5)
    store = SimpleNamespace(due_candidates=AsyncMock(return_value=[1]))

    async def maintain():
        entered.set()
        await resume.wait()
        return 0, 0

    store.maintain = maintain
    processor = SimpleNamespace(halted=False, process=AsyncMock())
    handle = ZaloRuntime(store=store, processor=processor, settings=cfg)

    async def stop():
        processor.halted = True
        await dispatch.schedule(555)
        assert not handle.signal.is_set()
        resume.set()

    processor.stop = stop
    dispatch.set_scheduler(handle.wake)
    handle.task = asyncio.create_task(handle.run())
    await asyncio.wait_for(entered.wait(), timeout=1)
    # stop() deliberately clears the signal set by the runtime to inspect reset.
    original_stop = processor.stop

    async def observed_stop():
        handle.signal.clear()
        await original_stop()

    processor.stop = observed_stop
    await runtime.stop_zalo_runtime(handle)
    store.due_candidates.assert_not_called()
    processor.process.assert_not_called()
    assert handle.task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "invalid-config", "installed", "schema-failure"])
async def test_actual_lifespan_isolates_zalo_and_closes_after_stop(monkeypatch, mode):
    from fastapi import FastAPI
    from api.infrastructure.config import config
    from api.infrastructure.adapters import postgres_zalo_events
    from api.interfaces.api import main

    order = []
    cfg = settings(zalo_bot_enabled=mode != "disabled")
    if mode == "invalid-config":
        cfg.zalo_bot_lease_seconds = 105
    monkeypatch.setattr(config, "get_settings", lambda: cfg)
    monkeypatch.setattr(main, "_wire_postgres_rate_limit_port", AsyncMock())
    monkeypatch.setattr(main, "_maybe_start_rag_prewarm", lambda: None)
    monkeypatch.setattr(main, "_maybe_start_lead_mirror_reconciliation", lambda: None)

    async def retention():
        await asyncio.Event().wait()

    async def close():
        order.append("pools")

    monkeypatch.setattr(main, "_chat_history_retention_loop", retention)
    monkeypatch.setattr(main, "_close_persistence_pools", close)
    store = SimpleNamespace(maintain=AsyncMock(return_value=(0, 0)), due_candidates=AsyncMock(return_value=[]))

    async def schema():
        order.append("schema")
        if mode == "schema-failure":
            raise RuntimeError("private schema detail")

    store.validate_schema = schema
    monkeypatch.setattr(postgres_zalo_events, "PostgresZaloEventStore", lambda **kwargs: store)
    processor = SimpleNamespace(halted=False, process=AsyncMock())

    async def stop():
        processor.halted = True
        order.append("stop")
        assert dispatch.get_scheduler() != app.state.zalo_runtime.wake

    processor.stop = stop
    build = lambda **kwargs: processor
    monkeypatch.setattr(runtime, "build_zalo_processor", build)
    app = FastAPI()
    with pytest.raises(RuntimeError, match="request failure"):
        async with main._lifespan(app):
            assert (app.state.zalo_runtime is not None) == (mode == "installed")
            raise RuntimeError("request failure")
    assert app.state.zalo_runtime is None
    assert order == {
        "disabled": ["pools"], "invalid-config": ["pools"],
        "schema-failure": ["schema", "pools"], "installed": ["schema", "stop", "pools"],
    }[mode]


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_enabled", [True, False])
async def test_lifespan_retention_setting_controls_customer_history_cleanup(
    monkeypatch, retention_enabled
):
    from fastapi import FastAPI
    from api.infrastructure.adapters import postgres_chat_history
    from api.infrastructure.config import config
    from api.interfaces.api import main

    cfg = settings(zalo_bot_enabled=False, chat_history_retention_enabled=retention_enabled)
    monkeypatch.setattr(config, "get_settings", lambda: cfg)
    monkeypatch.setattr(main, "get_cfg", lambda key, default=None: getattr(cfg, key, default))
    monkeypatch.setattr(main, "_wire_postgres_rate_limit_port", AsyncMock())
    monkeypatch.setattr(main, "_maybe_start_rag_prewarm", lambda: None)
    monkeypatch.setattr(main, "_maybe_start_lead_mirror_reconciliation", lambda: None)
    monkeypatch.setattr(main, "_close_persistence_pools", AsyncMock())
    cleanup = AsyncMock(return_value=0)
    monkeypatch.setattr(postgres_chat_history.repository, "cleanup", cleanup)

    app = FastAPI()
    async with main._lifespan(app):
        await asyncio.sleep(0)

    if retention_enabled:
        cleanup.assert_awaited_once_with(retention_days=30)
    else:
        cleanup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("maintenance_enabled", [True, False])
async def test_zalo_sweeps_preserve_new_events_when_maintenance_is_disabled(
    maintenance_enabled
):
    processed = []
    cfg = SimpleNamespace(
        zalo_bot_enabled=True,
        zalo_bot_batch_size=5,
        zalo_bot_poll_seconds=0,
        zalo_bot_maintenance_enabled=maintenance_enabled,
    )
    processor = SimpleNamespace(halted=False)
    store = SimpleNamespace(
        maintain=AsyncMock(return_value=(0, 0)),
        due_candidates=AsyncMock(return_value=[42]),
    )

    async def process(event_id):
        processed.append(event_id)
        processor.halted = True

    processor.process = process
    await ZaloRuntime(store=store, processor=processor, settings=cfg).run()

    assert processed == [42]
    if maintenance_enabled:
        store.maintain.assert_awaited_once()
    else:
        store.maintain.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_ready_ignores_provider_failure(monkeypatch):
    from contextlib import asynccontextmanager
    from fastapi.routing import iter_route_contexts
    from api.application.services import rag_leg, sql_leg
    from api.infrastructure.config import config
    from api.infrastructure.adapters import zalo_bot_sender
    from api.interfaces.api import main

    monkeypatch.setattr(config, "export_runtime_env", lambda: None)
    monkeypatch.setattr(rag_leg, "LIGHTRAG_READY", True)
    monkeypatch.setattr(zalo_bot_sender.HttpxZaloBotSender, "send_text", AsyncMock(side_effect=RuntimeError("provider down")))

    @asynccontextmanager
    async def acquire():
        yield SimpleNamespace(fetchval=AsyncMock(return_value=1))

    monkeypatch.setattr(sql_leg, "get_ro_pool", AsyncMock(return_value=SimpleNamespace(acquire=acquire)))
    app = main.create_app()
    ready = next(route.endpoint for route in iter_route_contexts(app.routes) if route.path == "/ready")
    result = await ready()
    assert result["ok"] is True
    assert result["pg"] is True
    assert result["lightrag"] is True
    assert "provider" not in result
    zalo_bot_sender.HttpxZaloBotSender.send_text.assert_not_called()
