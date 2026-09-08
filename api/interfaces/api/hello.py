"""First-open greeting endpoint for the chat widget.

Generates a grounded greeting (project intro + need-discovery) with a single
direct LLM call, reusing the shared chat adapter via ``get_llm()``. Identity is
project-scoped (story 10.2): the system prompt and fallback greeting carry
{ten_thuong_mai}/{vi_tri} placeholders rendered against the registry at request
time. Degrades to a static grounded greeting so FE always receives a first
message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.application.services.image_search import search_project_images
from api.application.services.media_config import (
    fetch_recent_project_images,
    list_project_videos,
)
from api.application.services.project_config import (
    DEFAULT_PROJECT_KEY,
    bound_request_project,
    load_project_registry_record,
    render_template,
)
from api.application.services.project_scope import (
    ProjectScopeError,
    resolve_project_key,
)
from api.application.services.sales_kit import sales_kit_block
from api.infrastructure.dependencies import get_llm
from api.infrastructure.ports.firebase_auth import FirebaseAuthTokenError

logger = logging.getLogger("api.hello")

router = APIRouter(tags=["hello"])

GREETING_TIMEOUT_S = 6.0  # Inner LLM timeout; the outer deadline bounds the whole response.
GREETING_MAX_TOKENS = 400
HELLO_IMAGE_FALLBACK_LIMIT = 8  # published-rows cap when vector search is unavailable


def _hello_deadline_s() -> float:
    """Read the total greeting deadline without coupling this route to settings internals."""
    try:
        value = float(os.getenv("HELLO_DEADLINE_S", "3.0"))
    except (TypeError, ValueError):
        return 3.0
    return max(value, 0.001)


def _hello_media_budget_s() -> float:
    """Budget for resolving greeting media before the LLM call (FR-34).

    Env-overridable like the total deadline so slow vector/DB paths can be
    tuned per deployment without a code change.
    """
    try:
        value = float(os.getenv("HELLO_MEDIA_BUDGET_S", "2.0"))
    except (TypeError, ValueError):
        return 2.0
    return max(value, 0.001)

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
    audience: str = "customer"
    suggestions: list[str] = Field(default_factory=list)
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
    "6. Lời chào ngắn gọn, không vượt quá 60 từ.\n"
    "7. Chỉ trả về nội dung lời chào, không kèm giải thích hay dẫn nguồn."
)

# Grounded from the project registry (story 10.2): identity placeholders render
# per project so a Soleil first-open never greets as Camellia.
_CUSTOMER_SUGGESTIONS = {
    "camellia": [
        "Giá tốt nhất và ưu đãi Camellia hiện tại thế nào?",
        "Pháp lý Camellia đã minh bạch đến đâu?",
        "Cho em xem ảnh và mặt bằng Camellia",
        "Đầu tư cho thuê Camellia có phù hợp không?",
    ],
    "soleil": [
        "Giá tốt nhất và ưu đãi Soleil hiện tại thế nào?",
        "Pháp lý Soleil đã minh bạch đến đâu?",
        "Cho em xem ảnh và mặt bằng Soleil",
        "Đầu tư cho thuê Soleil có phù hợp không?",
    ],
}
_SALES_SUGGESTIONS = {
    "camellia": [
        "Tra giá và chính sách Camellia",
        "Tra pháp lý Camellia",
        "Xem hình ảnh và mặt bằng Camellia",
    ],
    "soleil": [
        "Tra giá và chính sách Soleil",
        "Tra pháp lý Soleil",
        "Xem hình ảnh và mặt bằng Soleil",
    ],
}


def _audience_content(project_key: str | None, audience: str) -> tuple[str, list[str]]:
    key = (project_key or DEFAULT_PROJECT_KEY).lower()
    if audience == "sales":
        return (
            "Chào anh/chị, em hỗ trợ tra cứu nội bộ. Anh/chị có thể hỏi mẫu về giá, chính sách, pháp lý, mặt bằng và hình ảnh của dự án đang chọn.",  # noqa: E501
            _SALES_SUGGESTIONS.get(
                key, [f"Tra giá và chính sách {key}", f"Tra pháp lý {key}", f"Xem hình ảnh {key}"]
            ),
        )
    return (
        # FR-34: short sales-oriented static greeting (2-3 sentences) so the
        # degrade path stays snappy; keeps the "em chào Anh/Chị" marker the
        # bearer-audience tests assert on.
        (
            f"Anh/Chị ơi, em chào Anh/Chị! Em chuyên viên tư vấn dự án {key.title()}. "
            "Anh/Chị muốn xem giá, ảnh mặt bằng hay pháp lý của dự án ạ? "
            "Để lại số điện thoại, chuyên viên sẽ tư vấn 1-1 và gửi lựa chọn phù hợp ngay nhé."
        ),
        _CUSTOMER_SUGGESTIONS.get(
            key,
            [
                f"Giá tốt nhất và ưu đãi {key} hiện tại thế nào?",
                f"Pháp lý {key} đã minh bạch đến đâu?",
                f"Cho em xem ảnh và mặt bằng {key}",
                f"Đầu tư cho thuê {key} có phù hợp không?",
            ],
        ),
    )


_FALLBACK_GREETING = (
    "Anh/Chị ơi, em chào Anh/Chị! Em là chuyên viên tư vấn dự án {ten_thuong_mai}. "
    "Dự án nằm tại {vi_tri}, cùng view và tiện ích nổi bật phục vụ cả gia đình. "
    "Anh/Chị đang quan tâm theo hướng để ở, đầu tư, cho thuê, hay làm văn phòng/khách sạn ạ? "
    "Anh/Chị nhắn nhu cầu, em sẽ tư vấn chi tiết và hướng dẫn Anh/Chị chọn căn phù hợp để sở hữu ngay nhé."  # noqa: E501
)


async def _resolve_audience(request: Request) -> str:
    """Resolve the greeting audience from a presented bearer token.

    Only a MISSING Authorization header selects the anonymous (customer)
    audience. Any presented credential that cannot be honored — non-bearer
    scheme, empty token, or a malformed/expired/foreign-signed ID token — is a
    hard 401 (never a silent downgrade to the anonymous greeting). Reuses the
    shared Firebase JWKS verifier port; no JWT logic lives here.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization:
        return "customer"
    if not authorization.lower().startswith("bearer ") or not authorization[7:].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization bearer credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    from api.infrastructure.dependencies import get_firebase_auth_verifier  # noqa: PLC0415

    try:
        verified = await get_firebase_auth_verifier().verify_id_token(
            authorization[7:].strip()
        )
    except FirebaseAuthTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid Firebase ID token: {exc.reason}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return "sales" if verified.role in {"sales", "admin"} else "customer"


def _sanitize_greeting(text: str) -> str:
    """Strip display-only characters forbidden by project rules (em/en-dash)."""
    return (text or "").replace(_EM_DASH, "-").replace(_EN_DASH, "-").strip()


def _build_messages(project_key: str | None = None) -> list[dict]:
    """System instruction + delimiter-wrapped grounded SALES_CONTEXT.

    ``project_key`` (story 10.2) renders the identity placeholders against the
    project registry; None keeps the legacy default identity.
    """
    return [
        {"role": "system", "content": render_template(_SYSTEM_PROMPT, project_key)},
        {"role": "user", "content": sales_kit_block(project_key)},
    ]


async def _resolve_hello_media(
    project_key: str | None, scope_failed: bool
) -> tuple[list[dict], list[dict]]:
    """Resolve greeting media (FR-34): images then videos, project-scoped.

    Images ride the semantic search -> recent-rows fallback chain; a scope
    failure skips the fetch entirely so no cross-project media can leak.
    Videos come from the sync project registry/config read.
    """
    if project_key:
        images = await search_project_images(project_key=project_key)
    elif not scope_failed:
        images = await search_project_images()
    else:
        images = []
    if not images and project_key:
        images = await fetch_recent_project_images(
            project_key, limit=HELLO_IMAGE_FALLBACK_LIMIT
        )
    videos = list_project_videos(project_key or DEFAULT_PROJECT_KEY)
    return images, videos


async def _assemble_hello(
    trace_id: str,
    payload: HelloRequest | None,
    project_key: str | None,
    scope_failed: bool,
    audience: str,
    images: list[dict] | None = None,
    videos: list[dict] | None = None,
) -> HelloResponse:
    """Assemble the greeting, reusing pre-resolved media when provided.

    ``images``/``videos`` are injected by ``llms_hello`` after the media phase
    so the LLM timeout path can still attach the already-resolved media
    without a second fetch (FR-34); None means legacy self-resolution.
    """
    if images is None or videos is None:
        resolved_images, resolved_videos = await _resolve_hello_media(
            project_key, scope_failed
        )
        images = resolved_images if images is None else images
        videos = resolved_videos if videos is None else videos
    record = await load_project_registry_record(project_key)
    with bound_request_project(record):
        greeting, suggestions = _audience_content(project_key, audience)
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
    return HelloResponse(
        greeting=greeting,
        trace_id=trace_id,
        audience=audience,
        suggestions=suggestions,
        images=images,
        videos=videos,
    )


@router.post("/llms-hello", response_model=HelloResponse)
async def llms_hello(request: Request, payload: HelloRequest | None = None) -> HelloResponse:
    """Return an audience-specific greeting, bounded by a total response deadline.

    Media resolves FIRST inside its own budget (FR-34) so a slow or timed-out
    LLM never discards already-resolved imagery/videos: on any media failure
    the images degrade to [] and the sync video list is kept.
    """
    trace_id = "t-" + uuid.uuid4().hex[:10]
    audience = await _resolve_audience(request)
    project_key: str | None = None
    scope_failed = False
    if payload is not None:
        try:
            project_key = await resolve_project_key(payload.project_key)
        except ProjectScopeError as exc:
            logger.warning("llms-hello project resolution failed: %s", exc)
            project_key = None
            scope_failed = True
    deadline = _hello_deadline_s()
    started = asyncio.get_running_loop().time()
    try:
        images, videos = await asyncio.wait_for(
            _resolve_hello_media(project_key, scope_failed),
            timeout=_hello_media_budget_s(),
        )
    except Exception:  # noqa: BLE001 — media is best-effort; TimeoutError included
        logger.warning("llms-hello media resolution failed or timed out; images=[]")
        # Videos are a cheap sync config read, so resolve them once more here
        # rather than losing them to a slow image search.
        images, videos = [], list_project_videos(project_key or DEFAULT_PROJECT_KEY)
    try:
        response = await asyncio.wait_for(
            _assemble_hello(
                trace_id,
                payload,
                project_key,
                scope_failed,
                audience,
                images=images,
                videos=videos,
            ),
            timeout=max(deadline - (asyncio.get_running_loop().time() - started), 0.001),
        )
    except asyncio.TimeoutError:
        greeting, suggestions = _audience_content(project_key, audience)
        logger.warning("llms-hello total deadline exceeded; using static greeting")
        # FR-34: the degrade response retains the media resolved before the
        # LLM phase instead of dropping it.
        response = HelloResponse(
            greeting=greeting,
            trace_id=trace_id,
            audience=audience,
            suggestions=suggestions,
            images=images,
            videos=videos,
        )
    if payload is not None and payload.session_id:
        logger.debug("llms-hello trace_id=%s session_id=%s", trace_id, payload.session_id)
    return response


def _frame(event: str, data: dict) -> str:
    """Serialize one SSE frame (event + data)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_greeting(
    session_id: str | None,
    project_key: str | None,
    scope_failed: bool = False,
    audience: str = "customer",
) -> AsyncIterator[str]:
    """Yield the greeting as token events, then a done event with trace_id.

    Streams the LLM response character-by-character on a token boundary so the
    frontend can render the greeting progressively, exactly like /api/query.
    Falls back to the static greeting streamed the same way on any failure.
    The registry record is read (async, once) inside the generator because the
    body only starts iterating after the handler has already returned.
    """
    trace_id = "t-" + uuid.uuid4().hex[:10]
    record = await load_project_registry_record(project_key)
    with bound_request_project(record):
        greeting = render_template(_FALLBACK_GREETING, project_key)
        audience_greeting, suggestions = _audience_content(project_key, audience)
        greeting = audience_greeting
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
                yield _frame(
                    "error", {"message": "Lời chào tạo nhanh không khả dụng; dùng lời chào mẫu."}
                )
            yield _frame("token", {"text": greeting})
        # Representative project imagery rides along with the welcome; a failure
        # only omits images from the stream, never the greeting itself. The
        # project predicate rides in the search SQL (M6); a scope failure skips
        # the fetch entirely so no cross-project media can leak. Same embedding-
        # outage fallback as the JSON variant keeps the gallery decorated.
        if project_key:
            images = await search_project_images(project_key=project_key)
        elif not scope_failed:
            images = await search_project_images()
        else:
            images = []
        if not images and project_key:
            images = await fetch_recent_project_images(
                project_key, limit=HELLO_IMAGE_FALLBACK_LIMIT
            )
        yield _frame("images", {"images": images})
        # Attach the project video registry as its own SSE event so the widget
        # can render playback without waiting for the done frame; resolved from
        # the request-bound registry record (async read above, M11).
        videos = list_project_videos(project_key or DEFAULT_PROJECT_KEY)
        yield _frame("videos", {"videos": videos})
    yield _frame("done", {"trace_id": trace_id})


@router.post("/llms-hello/stream")
async def llms_hello_stream(
    request: Request, payload: HelloRequest | None = None
) -> StreamingResponse:
    """Stream the first-open greeting as SSE (token events + done)."""
    audience = await _resolve_audience(request)
    session_id = payload.session_id if payload is not None else None
    project_key: str | None = None
    scope_failed = False
    if payload is not None:
        try:
            project_key = await resolve_project_key(payload.project_key)
        except ProjectScopeError as exc:
            logger.warning("llms-hello/stream project resolution failed: %s", exc)
            project_key = None
            scope_failed = True
    return StreamingResponse(
        _stream_greeting(session_id, project_key, scope_failed=scope_failed, audience=audience),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
