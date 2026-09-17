"""Authenticated Zalo Bot webhook ingress (ZB-01).

The route performs strict ordering:
  1. Constant-time secret check (fail closed).
  2. Media type (415), Content-Length (400/413), bounded body read (413).
  3. JSON parse (400 on malformed).
  4. Disabled flag -> 2xx zero work.
  5. Project to a (chat_id, message_id, event_name, text) tuple only
     when ok is True, event_name == "message.text.received", chat type is
     PRIVATE, chat.id / message_id / text are non-empty, and len(text) <=
     MAX_QUERY_LENGTH.
  6. Persist via the event store; on NEW event, fire-and-forget dispatch
     schedule() so the webhook ACK never waits for RAG.
  7. DB failure -> 503.

No inline RAG and no provider outbound call anywhere in this module.
Accepted means the pending row is durably persisted, not processed. Dispatch
is best effort: Cloud Run request-scoped CPU may stop after the response.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from api.application.services.zalo_bot_dispatch import schedule
from api.application.services.zalo_bot_projection import project_zalo_private_text_event
from api.domain.value_objects.constants import MAX_QUERY_LENGTH
from api.infrastructure.config.config import get_settings

logger = logging.getLogger("api.zalo_bot_routes")

router = APIRouter(
    prefix="/api/integrations/zalo-bot",
    tags=["zalo-bot"],
)

_SECRET_HEADER = "X-Bot-Api-Secret-Token"

# Retain strong references to fire-and-forget scheduler tasks so CPython does
# not garbage-collect them mid-execution (asyncio.create_task docs warn about
# exactly this). The done-callback discards the reference and retrieves the
# exception so scheduler failures are observable, never silently lost.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """Best-effort fire-and-forget task: retained reference + exception logging."""
    try:
        task = asyncio.create_task(coro)
    except Exception:
        coro.close()
        raise
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_task_exception)
    return task


def _log_task_exception(task: asyncio.Task) -> None:
    """Log a scheduler task failure (safe fields only; never chat content)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    # Log the failure type only: exception text can carry provider payloads,
    # chat text, or secrets. ZB-01 logs safe fields only.
    logger.warning("Zalo event dispatch failed: %s", type(exc).__name__)


def get_zalo_event_store():
    """FastAPI dependency that resolves the event persistence seam.

    Tests override this via app.dependency_overrides to inject a fake store
    instead of the real Postgres adapter (no DB needed).
    """
    from api.infrastructure.adapters.postgres_zalo_events import insert_event

    return insert_event


class _BoundedBodyError(Exception):
    """Raised when the raw body exceeds the configured byte cap."""


async def _recv_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Read the raw body with a hard byte cap, stopping the read at the cap.

    Reads incrementally from request.stream() so a chunked body without
    Content-Length cannot force the process to buffer an arbitrarily large body
    before the cap is evaluated. Raises _BoundedBodyError once the cap is
    exceeded, having consumed as few chunks as possible.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise _BoundedBodyError(total)
        chunks.append(chunk)
    return b"".join(chunks)


class _DuplicateKeyError(ValueError):
    """JSON object has ambiguous repeated keys."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError("Duplicate JSON object key")
        result[key] = value
    return result


@router.post("/webhook")
async def zalo_webhook(
    request: Request,
    event_store=Depends(get_zalo_event_store),
) -> JSONResponse:
    """Handle an authenticated Zalo Bot webhook."""
    settings = get_settings()

    # 1. Constant-time secret check FIRST, before any side effect.
    provided = request.headers.get(_SECRET_HEADER, "")
    expected = settings.zalo_bot_webhook_secret.get_secret_value()
    if not expected or not hmac.compare_digest(
        provided.encode("latin-1"), expected.encode("utf-8")
    ):
        if not expected:
            logger.warning(
                "Zalo webhook rejected: configured secret is empty (fail closed)"
            )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN, content={"ok": False}
        )

    content_type = request.headers.get("content-type")
    if content_type is not None and content_type.split(";", 1)[0].strip().lower() != "application/json":
        return JSONResponse(status_code=415, content={"ok": False})

    # 2. Reject declared-oversized BEFORE reading the body, then read bounded.
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            length = int(declared_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"ok": False})
        if length < 0:
            return JSONResponse(status_code=400, content={"ok": False})
        if length > settings.zalo_bot_max_body_bytes:
            return JSONResponse(status_code=413, content={"ok": False})

    try:
        raw_body = await _recv_bounded_body(request, settings.zalo_bot_max_body_bytes)
    except _BoundedBodyError:
        return JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, content={"ok": False}
        )

    # 3. Malformed JSON -> 400, never 500.
    try:
        parsed = json.loads(raw_body, object_pairs_hook=_unique_object)
    except _DuplicateKeyError:
        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"ok": True, "accepted": False}
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"ok": False}
        )

    # 4. Disabled but authenticated + valid -> 2xx zero work.
    if not settings.zalo_bot_enabled:
        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"ok": True, "accepted": False}
        )

    # 5. Project: extract a work tuple only for the supported event shape.
    event = project_zalo_private_text_event(parsed)
    if event is None or len(event.text) > MAX_QUERY_LENGTH:
        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"ok": True, "accepted": False}
        )

    # 6. Persist; on NEW event, fire-and-forget dispatch so the ACK never waits
    #    for RAG or provider outbound. The task is retained in _background_tasks
    #    to prevent GC and its exception is logged if it fails.
    try:
        event_id = await event_store(
            chat_id=event.chat_id,
            message_id=event.message_id,
            event_name=event.event_name,
            text=event.text,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Zalo webhook DB failure; event not persisted: %s", type(exc).__name__
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"ok": False}
        )

    if event_id is not None:
        # Durable pending row is the correctness boundary; dispatch cannot
        # undo acceptance, even if task setup fails before the callback exists.
        try:
            _spawn(schedule(event_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Zalo event dispatch setup failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"ok": True, "accepted": True}
        )

    # Duplicate event -> 2xx, scheduler NOT called a second time.
    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"ok": True, "accepted": False}
    )
