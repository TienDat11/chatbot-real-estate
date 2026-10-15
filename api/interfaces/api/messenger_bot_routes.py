"""Authenticated Messenger Bot webhook ingress.

The route performs strict ordering:
  1. GET /webhook: hub.challenge handshake (Meta verification, Zalo has none).
  2. POST /webhook media type (415), Content-Length (400/413), bounded body (413).
  3. X-Hub-Signature-256 over the RAW buffered bytes (403 fail-closed).
  4. JSON parse (400 malformed; 2xx-accepted=False on duplicate keys).
  5. Disabled flag -> 2xx zero work.
  6. Project to a LIST of (psid, event_ref, kind, text) tuples, rejecting any
     entry whose id is not the configured Page (WRONG_PAGE); persist each
     accepted event via the event store; on NEW event, fire-and-forget dispatch
     schedule() so the webhook ACK never waits for RAG.
  7. Classify every persist failure. A transient storage fault -> 503 so Meta
     redelivers the batch; a deterministic data fault -> per-event reject while
     the batch still answers 2xx, because a redelivery of data that can never be
     stored would repeat the failure forever.

No inline RAG and no provider outbound call anywhere in this module.
Accepted means the pending row is durably persisted, not processed. Dispatch
is best effort: Cloud Run request-scoped CPU may stop after the response.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse

from api.application.services.messenger_bot_dispatch import schedule
from api.application.services.messenger_bot_projection import (
    InboundReason,
    project_messenger_event,
)
from api.infrastructure.adapters.postgres_messenger_events import MessengerStorageError
from api.infrastructure.config.config import get_settings

logger = logging.getLogger("api.messenger_bot_routes")

router = APIRouter(
    prefix="/api/integrations/messenger-bot",
    tags=["messenger-bot"],
)

_SIGNATURE_HEADER = "X-Hub-Signature-256"

# The one storage code worth asking Meta to redeliver. Every other
# MessengerStorageError code is a deterministic fault (INVALID_EVENT,
# SCHEMA_INCOMPATIBLE, BACKFILL_REQUIRED, MESSENGER_DISABLED, IDENTITY_CONFLICT,
# INVALID_PROVIDER_ID): a redelivery would re-run the identical doomed
# statement, and Meta eventually unsubscribes a Page after sustained failures.
_TRANSIENT_STORAGE_CODE = "STORAGE_UNAVAILABLE"

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
    # chat text, or secrets. Ingress logs safe fields only.
    logger.warning("Messenger event dispatch failed: %s", type(exc).__name__)


_SHAPE_MAX_ENTRIES = 40
_SHAPE_MAX_DEPTH = 8
_SHAPE_MAX_KEY_LEN = 64
# Official Messenger webhook schema keys (from the payload contract this route
# is built against). Only these may be logged by name; any other key is
# attacker/caller-controlled and MUST be redacted to a structural placeholder.
_KNOWN_SCHEMA_KEYS = frozenset({
    "object", "entry", "id", "time", "messaging", "sender", "recipient",
    "message", "mid", "text", "quick_reply", "payload", "postback", "title",
    "timestamp", "attachments", "type", "referral", "ref", "sticker_id",
})
_REDACTED_KEY = "<key>"
_SAFE_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)


def _safe_key_path(path: str) -> str:
    """Sanitize one dotted key path; caller-controlled segments are redacted.

    Segments are logged by name only if they are in the official Messenger
    schema key set (never attacker-controlled). The only structural marker is
    the exact list-index suffix `[0]` emitted by the shape walker; the segment
    name before it is still allowlist-checked. Everything else becomes a
    placeholder: structure (presence, depth, type, length) stays diagnosable,
    values and unknown key names never reach logs.
    """
    out = []
    for segment in path[:_SHAPE_MAX_KEY_LEN].split("."):
        name, index = segment, ""
        if segment.endswith("[0]"):
            name, index = segment[:-3], "[0]"
        if name in _KNOWN_SCHEMA_KEYS:
            out.append("".join(ch for ch in name if ch in _SAFE_KEY_CHARS) + index)
        else:
            out.append(_REDACTED_KEY + index)
    return ".".join(out)


def _payload_shape(parsed: object) -> str:
    """Values-free structural skeleton of a parsed payload.

    Emits dotted key paths with VALUE TYPES ONLY (never values). Strings add a
    length only. Lists report their length and expand index 0 at most. Capped at
    _SHAPE_MAX_ENTRIES entries and _SHAPE_MAX_DEPTH depth. This is the only
    mechanism that may inspect the payload for logging; it must never return or
    log a value.
    """
    entries: list[str] = []

    def walk(node: object, path: str, depth: int) -> None:
        if len(entries) >= _SHAPE_MAX_ENTRIES:
            return
        key = _safe_key_path(path)
        if isinstance(node, dict):
            entries.append(f"{key}:dict" if key else "dict")
            if depth >= _SHAPE_MAX_DEPTH:
                return
            for name in sorted(node.keys(), key=str)[: _SHAPE_MAX_ENTRIES]:
                walk(node[name], f"{path}.{name}" if path else str(name), depth + 1)
            return
        if isinstance(node, list):
            entries.append(f"{key}:list(len={len(node)})")
            if node and depth < _SHAPE_MAX_DEPTH:
                walk(node[0], f"{path}[0]", depth + 1)
            return
        if isinstance(node, str):
            entries.append(f"{key}:str(len={len(node)})")
            return
        if isinstance(node, bool):
            entries.append(f"{key}:bool")
            return
        if isinstance(node, int):
            entries.append(f"{key}:int")
            return
        if isinstance(node, float):
            entries.append(f"{key}:float")
            return
        if node is None:
            entries.append(f"{key}:none")
            return
        entries.append(f"{key}:{type(node).__name__}")

    walk(parsed, "", 0)
    if len(entries) > _SHAPE_MAX_ENTRIES:
        entries = entries[:_SHAPE_MAX_ENTRIES] + ["+more"]
    return ",".join(entries)


def _log_ingress_reject(reason: InboundReason, parsed: object) -> None:
    """Log a projection rejection with a reason code and a values-free shape."""
    logger.warning(
        "MESSENGER_INGRESS_REJECT reason=%s shape=%s",
        reason.value,
        _payload_shape(parsed),
    )


def get_messenger_event_store():
    """FastAPI dependency that resolves the event persistence seam.

    Tests override this via app.dependency_overrides to inject a fake store
    instead of the real Postgres adapter (no DB needed).
    """
    from api.infrastructure.adapters.postgres_messenger_events import insert_event

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


def _single_query_param(query, name: str) -> str | None:
    """Return the single value for a query param, else None (missing or repeated)."""
    values = query.getlist(name)
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]


@router.get("/webhook")
async def messenger_webhook_verify(request: Request):
    """Answer the Meta hub.challenge handshake; never raise, never 500."""
    settings = get_settings()
    mode = _single_query_param(request.query_params, "hub.mode")
    challenge = _single_query_param(request.query_params, "hub.challenge")
    verify_token = _single_query_param(request.query_params, "hub.verify_token")
    expected = settings.messenger_verify_token.get_secret_value()
    if not expected or mode is None or not challenge or verify_token is None:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN, content={"ok": False}
        )
    try:
        matched = mode == "subscribe" and hmac.compare_digest(
            verify_token.encode("utf-8"), expected.encode("utf-8")
        )
    except (AttributeError, UnicodeError, TypeError):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN, content={"ok": False}
        )
    if matched:
        return PlainTextResponse(challenge, status_code=200)
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN, content={"ok": False}
    )


@router.post("/webhook")
async def messenger_webhook(
    request: Request,
    event_store=Depends(get_messenger_event_store),
) -> JSONResponse:
    """Handle an authenticated Messenger Bot webhook batch."""
    settings = get_settings()

    # 1. Media type BEFORE reading the body.
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
        if length > settings.messenger_max_body_bytes:
            return JSONResponse(status_code=413, content={"ok": False})

    try:
        raw_body = await _recv_bounded_body(request, settings.messenger_max_body_bytes)
    except _BoundedBodyError:
        return JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, content={"ok": False}
        )

    # 4. Signature over the RAW buffered bytes (never re-serialize). Fail closed.
    app_secret = settings.messenger_app_secret.get_secret_value()
    if not app_secret:
        logger.warning(
            "Messenger webhook rejected: configured app secret is empty (fail closed)"
        )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN, content={"ok": False}
        )
    try:
        expected_sig = "sha256=" + hmac.new(
            app_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        provided_sig = (request.headers.get(_SIGNATURE_HEADER) or "").encode("utf-8")
        signature_ok = hmac.compare_digest(
            expected_sig.encode("utf-8"), provided_sig
        )
    except (AttributeError, UnicodeError, TypeError):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN, content={"ok": False}
        )
    if not signature_ok:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN, content={"ok": False}
        )

    # 5. Malformed JSON -> 400, never 500.
    try:
        parsed = json.loads(raw_body, object_pairs_hook=_unique_object)
    except _DuplicateKeyError:
        logger.warning("MESSENGER_INGRESS_REJECT reason=duplicate_json_keys")
        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"ok": True, "accepted": False}
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"ok": False}
        )

    # 6. Disabled but authenticated + valid -> 2xx zero work.
    if not settings.messenger_bot_enabled:
        logger.warning("MESSENGER_INGRESS_REJECT reason=bot_disabled")
        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"ok": True, "accepted": False}
        )

    # 7. Batch: project to a list; persist each accepted event. A transient
    #    storage fault -> 503 at once so Meta redelivers the batch; a
    #    deterministic data fault -> reject that one event and keep going.
    results = project_messenger_event(parsed, page_id=settings.messenger_page_id)
    accepted_any = False
    for result in results:
        if result.event is None:
            _log_ingress_reject(result.reason, parsed)
            continue
        event = result.event
        try:
            event_id = await event_store(
                psid=event.psid,
                event_ref=event.event_ref,
                event_kind=event.kind,
                text=event.text,
                selected_project_key=event.selected_project_key,
            )
        except MessengerStorageError as exc:
            if exc.code != _TRANSIENT_STORAGE_CODE:
                # Deterministic: this payload can never be stored, so a Meta
                # redelivery would loop forever and eventually cost the Page
                # subscription. Drop the event, log the fixed code (never the
                # PSID or the text), and let the siblings in the batch land.
                logger.warning("MESSENGER_INGRESS_REJECT reason=%s", exc.code)
                continue
            logger.warning(
                "Messenger webhook DB failure; event not persisted: %s", exc.code
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"ok": False},
            )
        except Exception as exc:  # noqa: BLE001
            # Unclassified failure: assume transient and let Meta redeliver
            # rather than silently drop an event we cannot explain.
            logger.warning(
                "Messenger webhook DB failure; event not persisted: %s",
                type(exc).__name__,
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"ok": False},
            )
        if event_id is None:
            # Duplicate event -> scheduler NOT called a second time.
            logger.info("MESSENGER_INGRESS_DUPLICATE")
            continue
        # Durable pending row is the correctness boundary; dispatch cannot
        # undo acceptance, even if task setup fails before the callback exists.
        try:
            _spawn(schedule(event_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Messenger event dispatch setup failed: %s", type(exc).__name__
            )
        logger.info("MESSENGER_INGRESS_ACCEPTED event_id=%s", event_id)
        accepted_any = True

    # 8. At least one durable accept -> accepted=True; all reject/dedup -> False.
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"ok": True, "accepted": accepted_any},
    )
