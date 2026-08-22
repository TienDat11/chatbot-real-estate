"""First-open greeting endpoint for the chat widget.

Generates a grounded greeting (project intro + need-discovery) with a single
direct LLM call, reusing the shared chat adapter via ``get_llm()``. Identity is
project-scoped (story 10.2): the system prompt and fallback greeting carry
{ten_thuong_mai}/{vi_tri} placeholders rendered against the registry at request
time. Degrades to a static grounded greeting so FE always receives a first
message.
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
from api.application.services.project_config import DEFAULT_PROJECT_KEY, render_template
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
# {ten_thuong_mai}/{vi_tri} render against the project registry (story 10.2).
_SYSTEM_PROMPT = (
    "Bạn là chuyên viên tư vấn cao cấp của dự án {ten_thuong_mai}. "
    "Nhiệm vụ duy nhất: viết lời chào đầu tiên khi khách vừa mở khung chat.\n"
    "Yêu cầu cứng:\n"
    "1. Chỉ dùng số liệu có trong SALES_CONTEXT bên dưới. KHÔNG bịa số.\n"
    "2. Gọi khách là 'Anh/Chị', tự xưng 'em'. Giọng ấm, tự tin, gọn, như tư vấn trực tiếp.\n"
    "3. Lời chào phải đủ: chào khách; giới thiệu ngắn dự án (vị trí {vi_tri}; view và "
    "tiện ích nổi bật); "
    "hỏi nhu cầu thuộc 4 nhóm (để ở, đầu tư, cho thuê, làm văn phòng hoặc khách sạn); "
    "dẫn khách đi bước tiếp theo để mua bằng lời mời nhắn tư vấn, xem dự án hoặc đặt lịch hẹn.\n"
    "4. KHÔNG dùng dấu gạch ngang dài em-dash '—'; dùng dấu phẩy hoặc gạch ngang thường '-'.\n"
    "5. 80-180 từ, văn xuôi, không heading, không bảng, không danh sách đánh số.\n"
    "6. Chỉ trả về nội dung lời chào, không kèm giải thích hay dẫn nguồn."
)

# Grounded from the project registry (story 10.2): identity placeholders render
# per project so a Soleil first-open never greets as Camellia.
_FALLBACK_GREETING = (
    "Anh/Chị ơi, em chào Anh/Chị! Em là chuyên viên tư vấn dự án {ten_thuong_mai}. "
    "Dự án nằm tại {vi_tri}, cùng view và tiện ích nổi bật phục vụ cả gia đình. "
    "Anh/Chị đang quan tâm theo hướng để ở, đầu tư, cho thuê, hay làm văn phòng/khách sạn ạ? "
    "Anh/Chị nhắn nhu cầu, em sẽ tư vấn chi tiết và hướng dẫn Anh/Chị chọn căn phù hợp để sở hữu ngay nhé."
)


def _sanitize_greeting(text: str) -> str:
    """Strip display-only characters forbidden by project rules (em/en-dash)."""
    return (text or "").replace(_EM_DASH, "-").replace(_EN_DASH, "-").strip()


def _build_messages(project_key: "str | None" = None) -> list[dict]:
    """System instruction + delimiter-wrapped grounded SALES_CONTEXT.

    ``project_key`` (story 10.2) renders the identity placeholders against the
    project registry; None keeps the legacy default identity.
    """
    return [
        {"role": "system", "content": render_template(_SYSTEM_PROMPT, project_key)},
        {"role": "user", "content": sales_kit_block(project_key)},
    ]


@router.post("/llms-hello", response_model=HelloResponse)
async def llms_hello(payload: HelloRequest | None = None) -> HelloResponse:
    """Return an LLM-generated first greeting, falling back to a static one."""
    trace_id = "t-" + uuid.uuid4().hex[:10]
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
    greeting = render_template(_FALLBACK_GREETING, project_key)
    try:
        llm = get_llm()
        text = await llm.complete(
            _build_messages(project_key),
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
    videos = list_project_videos(project_key or DEFAULT_PROJECT_KEY)
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
    greeting = render_template(_FALLBACK_GREETING, project_key)
    try:
        llm = get_llm()
        tokens: list[str] = []
        async for token in llm.stream(
            _build_messages(project_key),
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
    videos = list_project_videos(project_key or DEFAULT_PROJECT_KEY)
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
