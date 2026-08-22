"""First-open greeting endpoint for the chat widget.

Generates a grounded greeting (The Camellia intro + need-discovery) with a
single direct LLM call, reusing the shared chat adapter via ``get_llm()``.
Degrades to a static grounded greeting so FE always receives a first message.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.application.services.image_search import search_project_images
from api.application.services.media_config import list_project_videos
from api.application.services.project_scope import (
    ProjectScopeError,
    filter_images_by_project,
    resolve_project_key,
)
from api.application.services.sales_kit import sales_kit_block
from api.infrastructure.dependencies import get_llm

logger = logging.getLogger("api.hello")

router = APIRouter(tags=["hello"])

GREETING_TIMEOUT_S = 6.0  # single LLM greeting call; 6s bounds a stuck gateway while leaving warm calls (sub-2s) ample headroom
GREETING_MAX_TOKENS = 400

# Em-dash is a display-only hard rule in this project; normalize it and the
# visually similar en-dash before returning any greeting string.
_EM_DASH = "\u2014"
_EN_DASH = "\u2013"


class HelloRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=128)
    # Story 10.1: the project the greeting is scoped to. Optional so the old
    # frontend keeps working; the default-rule resolves it when omitted.
    project_key: str | None = Field(default=None, max_length=64)
    # Anonymous identity (D7): UUID v4 from the client, used to prefix the
    # conversation context key (f"{device_id}:{session_id}").
    device_id: str | None = Field(default=None, max_length=64)


class HelloResponse(BaseModel):
    greeting: str
    trace_id: str
    # Representative project imagery accompanying the welcome message; best-effort
    # and omitted ([]-ish) on failure so the greeting contract never hard-fails.
    images: list[dict] = Field(default_factory=list)
    # Project videos (brand film + drone) attached to the welcome so the widget
    # can offer playback immediately; additive and best-effort like images.
    videos: list[dict] = Field(default_factory=list)


# System prompt mirrors system_policy.md Layer 0 (intro first) + Layer 4
# (four need groups) so the greeting stays consistent with /query voice.
_SYSTEM_PROMPT = (
    "Bạn là chuyên viên tư vấn cao cấp của dự án The Camellia Sơn Trà, Đà Nẵng. "
    "Nhiệm vụ duy nhất: viết lời chào đầu tiên khi khách vừa mở khung chat.\n"
    "Yêu cầu cứng:\n"
    "1. Chỉ dùng số liệu có trong SALES_CONTEXT bên dưới. KHÔNG bịa số.\n"
    "2. Gọi khách là 'Anh/Chị', tự xưng 'em'. Giọng ấm, tự tin, gọn, như tư vấn trực tiếp.\n"
    "3. Lời chào phải đủ: chào khách; giới thiệu ngắn dự án (vị trí giao lộ Lê Văn Lương - "
    "Lê Đức Thọ, Thọ Quang, Sơn Trà; view biển, view núi Sơn Trà; 42 tiện ích đa tầng); "
    "hỏi nhu cầu thuộc 4 nhóm (để ở, đầu tư, cho thuê, làm văn phòng hoặc khách sạn); "
    "dẫn khách đi bước tiếp theo để mua bằng lời mời nhắn tư vấn, xem dự án hoặc đặt lịch hẹn.\n"
    "4. KHÔNG dùng dấu gạch ngang dài em-dash '—'; dùng dấu phẩy hoặc gạch ngang thường '-'.\n"
    "5. 80-180 từ, văn xuôi, không heading, không bảng, không danh sách đánh số.\n"
    "6. Chỉ trả về nội dung lời chào, không kèm giải thích hay dẫn nguồn."
)

# Grounded from data/_processed/project_info.json + api/prompts/sales_kit_vn.md.
_FALLBACK_GREETING = (
    "Anh/Chị ơi, em chào Anh/Chị! Em là chuyên viên tư vấn dự án The Camellia Sơn Trà, Đà Nẵng. "
    "Dự án nằm ngay giao lộ Lê Văn Lương - Lê Đức Thọ, phường Thọ Quang, quận Sơn Trà, vừa gần biển "
    "vừa có view núi Sơn Trà, cùng 42 tiện ích đa tầng phục vụ cả gia đình. "
    "Anh/Chị đang quan tâm theo hướng để ở, đầu tư, cho thuê, hay làm văn phòng/khách sạn ạ? "
    "Anh/Chị nhắn nhu cầu, em sẽ tư vấn chi tiết và hướng dẫn Anh/Chị chọn căn phù hợp để sở hữu ngay nhé."
)


def _sanitize_greeting(text: str) -> str:
    """Strip display-only characters forbidden by project rules (em/en-dash)."""
    return (text or "").replace(_EM_DASH, "-").replace(_EN_DASH, "-").strip()


def _build_messages() -> list[dict]:
    """System instruction + delimiter-wrapped grounded SALES_CONTEXT."""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": sales_kit_block()},
    ]


@router.post("/llms-hello", response_model=HelloResponse)
async def llms_hello(payload: HelloRequest | None = None) -> HelloResponse:
    """Return an LLM-generated first greeting, falling back to a static one."""
    trace_id = "t-" + uuid.uuid4().hex[:10]
    greeting = _FALLBACK_GREETING
    project_key: str | None = None
    if payload is not None:
        try:
            project_key = await resolve_project_key(payload.project_key)
        except ProjectScopeError as exc:
            # Greeting is the first-open latch: a project-scope failure must not
            # 500 the widget, so the greeting falls back and images/videos are
            # omitted rather than leaking another project's media.
            logger.warning("llms-hello project resolution failed: %s", exc)
            project_key = None
    try:
        llm = get_llm()
        text = await llm.complete(
            _build_messages(),
            max_tokens=GREETING_MAX_TOKENS,
            timeout=GREETING_TIMEOUT_S,
        )
        candidate = _sanitize_greeting(text)
        if candidate:
            greeting = candidate
    except Exception as exc:  # noqa: BLE001 — greeting must never 500 the request
        logger.warning("llms-hello LLM failed; using static greeting: %s", exc)
    # Representative project imagery decorates the welcome; a failure here only
    # drops images, never the greeting itself.
    images = await search_project_images()
    if project_key:
        images = await filter_images_by_project(images, project_key)
    # Videos ride along with the imagery; list_project_videos is a frozen config
    # so this attach step cannot fail or block the greeting.
    videos = list_project_videos(project_key or "camellia")
    if payload is not None and payload.session_id:
        logger.debug("llms-hello trace_id=%s session_id=%s", trace_id, payload.session_id)
    return HelloResponse(greeting=greeting, trace_id=trace_id, images=images, videos=videos)


def _frame(event: str, data: dict) -> str:
    """Serialize one SSE frame (event + data)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_greeting(
    session_id: str | None, project_key: str | None
) -> AsyncIterator[str]:
    """Yield the greeting as token events, then a done event with trace_id.

    Streams the LLM response character-by-character on a token boundary so the
    frontend can render the greeting progressively, exactly like /api/query.
    Falls back to the static greeting streamed the same way on any failure.
    """
    trace_id = "t-" + uuid.uuid4().hex[:10]
    greeting = _FALLBACK_GREETING
    try:
        llm = get_llm()
        tokens: list[str] = []
        async for token in llm.stream(
            _build_messages(),
            max_tokens=GREETING_MAX_TOKENS,
        ):
            tokens.append(token)
            yield _frame("token", {"text": token})
        joined = _sanitize_greeting("".join(tokens))
        if joined:
            greeting = joined
    except Exception as exc:  # noqa: BLE001 — greeting must never fail the stream
        logger.warning("llms-hello/stream LLM failed; using static greeting: %s", exc)
        if session_id:
            yield _frame("error", {"message": "Lời chào tạo nhanh không khả dụng; dùng lời chào mẫu."})
        yield _frame("token", {"text": greeting})
    # Representative project imagery rides along with the welcome; a failure only
    # omits images from the stream, never the greeting itself.
    images = await search_project_images()
    if project_key:
        images = await filter_images_by_project(images, project_key)
    yield _frame("images", {"images": images})
    # Attach the project video registry as its own SSE event so the widget can
    # render playback without waiting for the done frame; frozen config, no I/O.
    videos = list_project_videos(project_key or "camellia")
    yield _frame("videos", {"videos": videos})
    yield _frame("done", {"trace_id": trace_id})


@router.post("/llms-hello/stream")
async def llms_hello_stream(payload: HelloRequest | None = None) -> StreamingResponse:
    """Stream the first-open greeting as SSE (token events + done)."""
    session_id = payload.session_id if payload is not None else None
    project_key: str | None = None
    if payload is not None:
        try:
            project_key = await resolve_project_key(payload.project_key)
        except ProjectScopeError as exc:
            logger.warning("llms-hello/stream project resolution failed: %s", exc)
            project_key = None
    return StreamingResponse(
        _stream_greeting(session_id, project_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
