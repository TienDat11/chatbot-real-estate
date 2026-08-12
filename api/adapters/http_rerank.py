"""HTTP rerank adapter — scores chunks via dashscope / aibox / Cohere-compatible endpoints.

Logic moved from the former api/rerank.py. Never raises: on any failure it marks
chunks degraded and returns them with their previous scores.
"""

from __future__ import annotations

import logging

import httpx

from api.constants import (
    DEFAULT_RERANK_MODEL,
    DEFAULT_RERANK_TIMEOUT_S,
    RERANK_ENDPOINT_AIBOX,
    RERANK_ENDPOINT_DASHSCOPE,
)
from api.ports.rerank import RerankPort

logger = logging.getLogger("api.adapters.http_rerank")


class HttpRerank(RerankPort):
    """Calls the provider rerank endpoint once per query over the retrieved chunks."""

    def __init__(self, api_key: str, base_url: str, binding: str, model: str = DEFAULT_RERANK_MODEL):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.binding = binding
        self.model = model

    async def rerank(self, query: str, chunks: list[dict]) -> list[dict]:
        """Update chunk scores from the rerank model; missing config -> unchanged + flag."""
        if not chunks:
            return chunks
        if not self.api_key or not self.base_url:
            return [_flag_degraded(c, "rerank_off") for c in chunks]

        endpoint = RERANK_ENDPOINT_DASHSCOPE if self.binding == "dashscope" else RERANK_ENDPOINT_AIBOX
        payload: dict = {
            "model": self.model,
            "query": query,
            "documents": [c.get("content", "") for c in chunks],
            "top_n": len(chunks),
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_RERANK_TIMEOUT_S) as client:
                resp = await client.post(self.base_url + endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — fail -> keep previous scores + flag
            logger.warning("rerank failed (%s) — keep previous scores", exc)
            return [_flag_degraded(c, "rerank_degraded") for c in chunks]

        results = data.get("results") or data.get("data") or (data.get("output") or {}).get("results") or []
        scores: dict[int, float] = {}
        for r in results:
            idx = r.get("index")
            score = r.get("relevance_score") if r.get("relevance_score") is not None else r.get("score")
            if idx is None and isinstance(r, dict):
                idx = r.get("id")  # DashScope sometimes keys results by 'id'
            if idx is not None and score is not None:
                try:
                    scores[int(idx)] = float(score)
                except (TypeError, ValueError):
                    continue

        return [{**c, "score": scores.get(i, c.get("score", 0.0))} for i, c in enumerate(chunks)]


def _flag_degraded(chunk: dict, flag: str) -> dict:
    """Mark a degradation flag on a chunk (merge strips underscore keys before UI)."""
    return {**chunk, f"_{flag}": True}
