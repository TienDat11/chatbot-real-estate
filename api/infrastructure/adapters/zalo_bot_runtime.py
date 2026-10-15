"""Guarded Zalo composition; importing this module never imports the core."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from api.application.services.zalo_bot_dispatch import reset_scheduler, set_scheduler
from api.application.services.zalo_bot_reconciliation import ZaloRuntime

if TYPE_CHECKING:
    from api.application.services.zalo_bot_processor import ZaloBotProcessor
    from api.infrastructure.adapters.postgres_zalo_events import PostgresZaloEventStore
    from api.infrastructure.config.config import Settings

logger = logging.getLogger("api.zalo_bot_runtime")


def build_zalo_processor(
    *, settings: Settings, store: PostgresZaloEventStore | None = None
) -> ZaloBotProcessor:
    """Compose the same shared pipeline as web, with immutable admission."""
    settings.validate_zalo_runtime()
    if not settings.zalo_bot_enabled:
        raise ValueError("ZALO_DISABLED")
    from api.application.pipelines.conv_workflow import RagQueryPipelineConv
    from api.application.services.chat_history_service import ChatHistoryService
    from api.application.services.global_assistant_service import GlobalAssistantService
    from api.application.services.zalo_bot_processor import ZaloBotProcessor
    from api.infrastructure.adapters.postgres_chat_history import repository
    from api.infrastructure.adapters.postgres_zalo_admission import PostgresZaloAdmission
    from api.infrastructure.adapters.postgres_zalo_events import PostgresZaloEventStore
    from api.infrastructure.adapters.zalo_bot_sender import HttpxZaloBotSender

    core = GlobalAssistantService(
        history=ChatHistoryService(repository),
        pipeline_factory=RagQueryPipelineConv,
    )
    return ZaloBotProcessor(
        store=store if store is not None else PostgresZaloEventStore(settings=settings),
        core=core,
        sender=HttpxZaloBotSender(token=settings.zalo_bot_token),
        identity_secret=settings.zalo_bot_identity_secret.get_secret_value(),
        admit=PostgresZaloAdmission(settings=settings).admit,
        settings=settings,
    )


def _observe_task(task: asyncio.Task) -> None:
    """Retrieve task failures, including late completion after bounded shutdown."""
    if not task.cancelled() and task.exception() is not None:
        logger.warning("ZALO_TASK_UNAVAILABLE")


async def start_zalo_runtime(settings: Settings) -> ZaloRuntime | None:
    """Install only after local validation and the durable cutover gate pass."""
    reset_scheduler()
    if not settings.zalo_bot_enabled:
        return None
    handle = None
    try:
        settings.validate_zalo_runtime()
        from api.infrastructure.adapters.postgres_zalo_events import PostgresZaloEventStore

        store = PostgresZaloEventStore(settings=settings)
        await store.validate_schema()
        processor = build_zalo_processor(settings=settings, store=store)
        handle = ZaloRuntime(store=store, processor=processor, settings=settings)
        handle.task = asyncio.create_task(handle.run(), name="zalo-bot-reconciliation")
        handle.task.add_done_callback(_observe_task)
        set_scheduler(handle.wake)
        if settings.zalo_bot_open_demo_access:
            logger.warning("ZALO_OPEN_DEMO_ACCESS_ENABLED")
        logger.info("ZALO_RUNTIME_INSTALLED")
        return handle
    except asyncio.CancelledError:
        await stop_zalo_runtime(handle)
        raise
    except Exception:
        await stop_zalo_runtime(handle)
        logger.warning("ZALO_RUNTIME_UNAVAILABLE")
        return None


async def stop_zalo_runtime(handle: ZaloRuntime | None) -> None:
    """Uninstall first; processor owns bounded cancellation and fenced release."""
    reset_scheduler()
    if handle is None or handle.stopping:
        return
    handle.stopping = True
    handle.signal.set()
    try:
        # stop sets halted before its first await; no following local event starts.
        await handle.processor.stop()
    except Exception:
        logger.warning("ZALO_STOP_UNAVAILABLE")
    finally:
        task = handle.task
        if task is not None:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=5)
            if not done:
                logger.warning("ZALO_STOP_UNAVAILABLE")
