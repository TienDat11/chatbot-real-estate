"""FastAPI app factory — thin routers, no business logic.

Routes:
  POST /query           — SSE when `Accept: text/event-stream`, else JSON
  POST /llms-hello      — first-open LLM greeting (router: api.interfaces.api.hello)
  GET  /health          — liveness
  GET  /ready           — PG reachable + LightRAG init flag
  GET  /sources/{doc_id}— registry metadata + validity status

Lifespan: no eager LightRAG init (pools are lazy; closed on shutdown).
SSE event order: routing -> places -> sources -> facts -> token -> done (error before done on failure).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from api import get_cfg
from api.domain.value_objects.constants import MAX_QUERY_LENGTH

logger = logging.getLogger("api.main")

# FE replays full assistant answers in `history`; this cap is DoS-only, the
# pipeline truncates each turn to MAX_QUERY_LENGTH before any LLM call.
MAX_HISTORY_CONTENT_LENGTH = 8000


# Typed request/response models.
class HistoryTurn(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1, max_length=MAX_HISTORY_CONTENT_LENGTH)


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)
    # Story 10.1: the question is scoped to one project. The FE contract marks
    # the field required, but the BE schema stays lenient so legacy clients
    # that omit it still reach the default-rule in resolve_project_key (exactly
    # one active project -> that project; >1 active -> 422 PROJECT_SCOPE).
    project_key: str | None = None
    session_id: str | None = None
    # D7: anonymous persistent device id (UUID v4) — stable context prefix.
    device_id: str | None = Field(default=None, max_length=64)
    as_of: str | None = None
    history: list[HistoryTurn] | None = None


class SourceItem(BaseModel):
    doc_id: str
    title: str
    section: str | None = None
    effective_from: str | None = None
    kind: str | None = None


class FactItem(BaseModel):
    fe_id: str
    subject: str | None = None
    policy_key: str | None = None
    fields: dict = Field(default_factory=dict)
    note: str | None = None


class PlaceItem(BaseModel):
    name: str
    kinds: list[str] = Field(default_factory=list)
    lat: float
    lng: float
    distance_m: float | None = None
    address: str | None = None
    rating: float | None = None


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceItem]
    facts: list[FactItem]
    places: list[PlaceItem] = Field(default_factory=list)
    confidence: str
    requires_review: bool
    routing: dict
    trace_id: str
    latency_ms: int
    conv_state: str | None = None
    conversation_directive: str | None = None
    slots_prefill: str | None = None


# SSE helpers.
def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _normalize_history(history: list[HistoryTurn] | None) -> list[dict[str, str]]:
    """Prepare replayed turns for the pipeline.

    FE replays full assistant answers, so each turn is truncated to
    MAX_QUERY_LENGTH and whitespace-only turns are dropped before any
    pipeline stage (rewrite/guard/answer) sees them. Role validity is
    already enforced by the HistoryTurn schema.
    """
    return [
        {"role": t.role, "content": t.content[:MAX_QUERY_LENGTH]}
        for t in (history or [])
        if t.content.strip()
    ]


async def _sse_stream(
    pipe, req: QueryRequest, as_of: str | None, project_key: str, device_id: str | None
) -> AsyncIterator[str]:
    """Run the pipeline emitting SSE; always emits `done` (even after errors)."""
    from api.application.pipelines.workflow import QueryRejected  # noqa: PLC0415

    q: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    async def on_event(event: str, data: dict) -> None:
        await q.put((event, data))

    history = _normalize_history(req.history)

    async def run_pipe() -> None:
        try:
            payload = await pipe.run(
                req.query, req.session_id, as_of, history,
                project_key=project_key, device_id=device_id, on_event=on_event,
            )
            await q.put(("__done__", payload))
        except QueryRejected as exc:
            await q.put(("__rejected__", {"message": exc.reason}))
        except Exception as exc:  # noqa: BLE001 — always emit error + done
            logger.exception("sse pipeline crashed")
            await q.put(("__crashed__", {"message": str(exc)}))

    # Emit ack immediately so FE shows zero-latency feedback (< 100ms)
    yield _frame("ack", {"received": True, "ts": int(asyncio.get_event_loop().time() * 1000)})
    task = asyncio.create_task(run_pipe())
    try:
        while True:
            event, data = await q.get()
            if event in ("__done__", "__rejected__", "__crashed__"):
                if event == "__rejected__":
                    yield _frame("error", {"message": data["message"]})
                    yield _frame("done", {})
                elif event == "__crashed__":
                    yield _frame("error", {"message": data["message"]})
                    yield _frame("done", {})
                else:
                    yield _frame("done", data)
                break
            yield _frame(event, data)
    finally:
        try:
            await task
        except Exception:  # noqa: BLE001
            logger.exception("sse task finalize")


# App factory.
def create_app() -> FastAPI:
    from api.infrastructure.config.config import export_runtime_env  # noqa: PLC0415

    export_runtime_env()
    app = FastAPI(title="rag-real-estate", version="0.1.0", docs_url="/docs", openapi_url="/openapi.json")

    # CORS from Settings ("*" default for internal MVP — tighten on deploy).
    origins = get_cfg("cors_origins", "*")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins if isinstance(origins, list) else [origins],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from api.interfaces.api.hello import router as hello_router  # noqa: PLC0415
    from api.interfaces.api.lead import router as lead_router  # noqa: PLC0415
    from api.interfaces.api.projects import router as projects_router  # noqa: PLC0415
    from api.interfaces.api.sales import router as sales_router  # noqa: PLC0415

    app.include_router(lead_router)
    app.include_router(sales_router)
    app.include_router(hello_router)
    app.include_router(projects_router)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        from api.application.services.audit import close_audit_pool  # noqa: PLC0415
        from api.domain.services.nl2sql_guard import close_nl2sql_pool  # noqa: PLC0415
        from api.application.services.sql_leg import close_ro_pool  # noqa: PLC0415
        from api.infrastructure.adapters.postgres_leads import close_lead_pool  # noqa: PLC0415

        for closer in (close_ro_pool, close_nl2sql_pool, close_audit_pool, close_lead_pool):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                logger.warning("pool close fail", exc_info=True)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "app": "rag-real-estate", "version": "0.1.0"}

    @app.get("/ready")
    async def ready() -> dict:
        """PG reachable + LightRAG init flag (set by rag_leg on successful get_lightrag)."""
        from api.application.services.rag_leg import LIGHTRAG_READY  # noqa: PLC0415
        from api.application.services.sql_leg import get_ro_pool  # noqa: PLC0415

        checks: dict = {"pg": False, "lightrag": LIGHTRAG_READY}
        try:
            pool = await get_ro_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks["pg"] = True
        except Exception as exc:  # noqa: BLE001
            checks["pg_error"] = str(exc)
        checks["ok"] = bool(checks["pg"] and checks["lightrag"])
        return checks

    @app.get("/sources/{doc_id}")
    async def source_info(doc_id: str) -> dict:
        """Registry metadata + validity status for one doc."""
        from api.application.services.sql_leg import get_ro_pool  # noqa: PLC0415

        pool = await get_ro_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT doc_id, kind, title, source_file, effective_from, effective_to, status, version, metadata "
                "FROM documents WHERE doc_id = $1",
                doc_id,
            )
        if not row:
            raise HTTPException(status_code=404, detail=f"doc {doc_id} không tồn tại")
        data = dict(row)
        data["effective_from"] = data["effective_from"].isoformat() if data.get("effective_from") else None
        data["effective_to"] = data["effective_to"].isoformat() if data.get("effective_to") else None
        return data

    @app.post("/query", response_model=QueryResponse)
    async def query(req: QueryRequest, request: Request) -> "StreamingResponse | dict":
        from api.application.pipelines.workflow import QueryRejected  # noqa: PLC0415
        from api.application.pipelines.conv_workflow import RagQueryPipelineConv  # noqa: PLC0415
        from api.application.services.project_scope import (  # noqa: PLC0415
            ProjectScopeError,
            resolve_project_key,
        )

        # Story 10.1 [RV-22/08]: resolve the active project BEFORE any leg runs —
        # >1 active project with no explicit choice is a 422 that prompts the
        # ProjectPicker; exactly one active project is the safe default.
        try:
            project_key = await resolve_project_key(req.project_key)
        except ProjectScopeError as exc:
            # Story 10.3: carry the pickable project list in the 422 body so the
            # FE popup renders immediately without a second round-trip to
            # GET /api/projects. Best-effort like the endpoint: a dead DB yields
            # projects: [] and the FE falls back to its static catalogue.
            from api.application.services.project_config import fetch_projects  # noqa: PLC0415

            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error": {"code": "PROJECT_SCOPE", "message": str(exc)},
                    "projects": fetch_projects(),
                },
            )

        accept = request.headers.get("accept") or ""
        if "text/event-stream" in accept:
            pipe = RagQueryPipelineConv()
            return StreamingResponse(
                _sse_stream(pipe, req, req.as_of, project_key, req.device_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # JSON mode
        pipe = RagQueryPipelineConv()
        try:
            payload = await pipe.run(
                req.query, req.session_id, req.as_of,
                _normalize_history(req.history),
                project_key=project_key,
                device_id=req.device_id,
            )
            return payload
        except QueryRejected as exc:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": {"code": "REJECTED", "message": exc.reason}},
            )
        except Exception as exc:  # noqa: BLE001 — never leak internal details
            logger.exception("query handler error")
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": {"code": "INTERNAL", "message": "internal error"}},
            )

    return app


app = create_app()
