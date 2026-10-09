"""Guarded Messenger composition; importing this module never imports the core."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from api.application.services.messenger_bot_dispatch import reset_scheduler, set_scheduler
from api.application.services.messenger_bot_reconciliation import MessengerRuntime

if TYPE_CHECKING:
    from api.application.services.messenger_bot_processor import MessengerBotProcessor
    from api.infrastructure.adapters.postgres_messenger_events import PostgresMessengerEventStore
    from api.infrastructure.config.config import Settings

logger = logging.getLogger("api.messenger_bot_runtime")


def build_messenger_processor(
    *, settings: Settings, store: PostgresMessengerEventStore | None = None
) -> MessengerBotProcessor:
    """Compose the same shared pipeline as web, with immutable admission."""
    settings.validate_messenger_runtime()
    if not settings.messenger_bot_enabled:
        raise ValueError("MESSENGER_DISABLED")
    from api.application.pipelines.conv_workflow import RagQueryPipelineConv
    from api.application.services.chat_history_service import ChatHistoryService
    from api.application.services.global_assistant_service import GlobalAssistantService
    from api.application.services.messenger_bot_processor import MessengerBotProcessor
    from api.infrastructure.adapters.messenger_bot_sender import HttpxMessengerBotSender
    from api.infrastructure.adapters.postgres_chat_history import repository
    from api.infrastructure.adapters.postgres_messenger_admission import PostgresMessengerAdmission
    from api.infrastructure.adapters.postgres_messenger_events import PostgresMessengerEventStore

    core = GlobalAssistantService(
        history=ChatHistoryService(repository),
        pipeline_factory=RagQueryPipelineConv,
    )
    return MessengerBotProcessor(
        store=store if store is not None else PostgresMessengerEventStore(settings=settings),
        core=core,
        sender=HttpxMessengerBotSender(
            page_id=settings.messenger_page_id,
            page_access_token=settings.messenger_page_access_token,
            api_base_url=settings.messenger_api_base_url,
            api_version=settings.messenger_graph_api_version,
            app_secret=settings.messenger_app_secret.get_secret_value(),
            appsecret_proof_enabled=settings.messenger_appsecret_proof_enabled,
        ),
        identity_secret=settings.messenger_identity_secret.get_secret_value(),
        admit=PostgresMessengerAdmission(settings=settings).admit,
        settings=settings,
        catalogue_loader=core._catalogue_loader,
    )


def _observe_task(task: asyncio.Task) -> None:
    """Retrieve task failures, including late completion after bounded shutdown."""
    if not task.cancelled() and task.exception() is not None:
        logger.warning("MESSENGER_TASK_UNAVAILABLE")


async def start_messenger_runtime(settings: Settings) -> MessengerRuntime | None:
    """Install only after local validation and the durable cutover gate pass."""
    reset_scheduler()
    if not settings.messenger_bot_enabled:
        return None
    handle = None
    try:
        settings.validate_messenger_runtime()
        from api.infrastructure.adapters.postgres_messenger_events import PostgresMessengerEventStore

        store = PostgresMessengerEventStore(settings=settings)
        await store.validate_schema()
        processor = build_messenger_processor(settings=settings, store=store)
        handle = MessengerRuntime(store=store, processor=processor, settings=settings)
        handle.task = asyncio.create_task(handle.run(), name="messenger-bot-reconciliation")
        handle.task.add_done_callback(_observe_task)
        set_scheduler(handle.wake)
        logger.info("MESSENGER_RUNTIME_INSTALLED")
        return handle
    except asyncio.CancelledError:
        await stop_messenger_runtime(handle)
        raise
    except Exception:
        await stop_messenger_runtime(handle)
        logger.warning("MESSENGER_RUNTIME_UNAVAILABLE")
        return None


async def stop_messenger_runtime(handle: MessengerRuntime | None) -> None:
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
        logger.warning("MESSENGER_STOP_UNAVAILABLE")
    finally:
        task = handle.task
        if task is not None:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=5)
            if not done:
                logger.warning("MESSENGER_STOP_UNAVAILABLE")
