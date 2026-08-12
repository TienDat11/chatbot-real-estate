"""FastAPI app factory — thin routers, no business logic.

Routes:
  POST /query           — SSE when `Accept: text/event-stream`, else JSON
  GET  /health          — liveness
  GET  /ready           — PG reachable + LightRAG init flag
  GET  /sources/{doc_id}— registry metadata + validity status

Lifespan: no eager LightRAG init (pools are lazy; closed on shutdown).
SSE event order: places -> sources -> facts -> token -> done (error before done on failure).
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
from api.constants import MAX_QUERY_LENGTH

logger = logging.getLogger("api.main")


# Typed request/response models.
class HistoryTurn(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1)


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)
    session_id: str | None = None
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


# SSE helpers.
def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _sse_stream(pipe, req: QueryRequest, as_of: str | None) -> AsyncIterator[str]:
    """Run the pipeline emitting SSE; always emits `done` (even after errors)."""
    from api.workflow import QueryRejected  # noqa: PLC0415

    q: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    async def on_event(event: str, data: dict) -> None:
        await q.put((event, data))

    history = [{"role": t.role, "content": t.content} for t in (req.history or [])]

    async def run_pipe() -> None:
        try:
            payload = await pipe.run(req.query, req.session_id, as_of, history, on_event=on_event)
            await q.put(("__done__", payload))
        except QueryRejected as exc:
            await q.put(("__rejected__", {"message": exc.reason}))
        except Exception as exc:  # noqa: BLE001 — always emit error + done
            logger.exception("sse pipeline crashed")
            await q.put(("__crashed__", {"message": str(exc)}))

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
    from api.config import export_runtime_env  # noqa: PLC0415

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

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        from api.audit import close_audit_pool  # noqa: PLC0415
        from api.nl2sql_guard import close_nl2sql_pool  # noqa: PLC0415
        from api.sql_leg import close_ro_pool  # noqa: PLC0415

        for closer in (close_ro_pool, close_nl2sql_pool, close_audit_pool):
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
        from api.rag_leg import LIGHTRAG_READY  # noqa: PLC0415
        from api.sql_leg import get_ro_pool  # noqa: PLC0415

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
        from api.sql_leg import get_ro_pool  # noqa: PLC0415

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
        from api.workflow import QueryRejected, RagQueryPipeline  # noqa: PLC0415

        accept = request.headers.get("accept") or ""
        if "text/event-stream" in accept:
            pipe = RagQueryPipeline()
            return StreamingResponse(
                _sse_stream(pipe, req, req.as_of),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # JSON mode
        pipe = RagQueryPipeline()
        try:
            payload = await pipe.run(
                req.query, req.session_id, req.as_of,
                [{"role": t.role, "content": t.content} for t in (req.history or [])],
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
