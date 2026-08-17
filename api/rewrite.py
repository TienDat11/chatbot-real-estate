"""Query rewrite + route + SQL spec — one LLM JSON-mode call.

Returns a RoutedResult (rewritten query, routing, sql_spec, keywords). Invalid
JSON retries once then falls back to rag-only. NL2SQL path is gated by a
deterministic aggregate-intent detector (LLM proposes, detector confirms).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.constants import LLM_CALL_TIMEOUT_S
from api.dependencies import llm, model_for_role

logger = logging.getLogger("api.rewrite")

# Deterministic keywords for high-stakes routing and NL2SQL gating.
HIGH_STAKES_KEYWORDS = (
    "cầm cố", "thế chấp", "chuyển nhượng", "công chứng", "quy hoạch", "thuế",
    "sổ đỏ", "giải chấp", "tranh chấp", "ủy quyền", "kê biên", "hiệu lực",
)

AGGREGATE_KEYWORDS = (
    "bao nhiêu căn", "mấy căn", "số lượng căn", "trung bình", "tổng", "tổng cộng",
    "so sánh", "đếm", "count", "average", "avg", "sum", "giá trên m2", "giá trên mét",
    "giá trung bình", "trung bình giá", "cao nhất", "thấp nhất", "nhiều nhất", "ít nhất",
)

# Canonical VN money parser (also used by guard/extract) — one implementation.
from api.price_calc import extract_budget, extract_price_intent

# Deterministic geo intent — ORed with the LLM routing decision so the maps leg
# fires on amenity/location queries even when the router omits needs_geo.
GEO_INTENT_KEYWORDS = (
    "gần chợ", "gần trường", "gần bệnh viện", "gần biển", "gần siêu thị",
    "gần công viên", "tiện ích", "xung quanh", "vị trí dự án", "khu vực dự án",
    "gần dự án", "quanh dự án",
)


def _has_geo_intent(query: str) -> bool:
    q = (query or "").lower()
    return any(k in q for k in GEO_INTENT_KEYWORDS)


def _geo_flag(routing_raw: Any, data: dict[str, Any], query: str) -> bool:
    """needs_geo = LLM routing flag ORed with a deterministic geo-intent check."""
    llm_flag = bool(routing_raw.get("needs_geo", False)) if isinstance(routing_raw, dict) else False
    if llm_flag:
        return True
    return _has_geo_intent(f"{query} {data.get('rewritten_query', '')}")


def detect_aggregate_intent(query: str) -> bool:
    """Deterministic aggregate/compare intent detector — the only NL2SQL gate."""
    q = (query or "").lower()
    return any(k in q for k in AGGREGATE_KEYWORDS)


# Few-shot prompt, read once at import.
_REWRITE_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "rewrite_fewshot.md"

_FEWSHOT: str = ""
if _REWRITE_PROMPT_PATH.exists():
    _FEWSHOT = _REWRITE_PROMPT_PATH.read_text(encoding="utf-8")
else:
    logger.warning("prompts/rewrite_fewshot.md chưa tồn tại — rewrite dùng rules mặc định")

_SYSTEM_REWRITE = (
    "Bạn là bộ định tuyến query cho chatbot bất động sản pháp lý Việt Nam. "
    "Nhiệm vụ: viết lại query cho tự chứa (self-contained), quyết định route (RAG/SQL), "
    "xây sql_spec nếu cần, liệt kê keywords. Trả về ĐÚNG MỘT JSON object, không kèm text khác."
)


@dataclass
class RoutedResult:
    rewritten: str
    routing: dict  # {needs_rag, needs_sql, structured_path}
    sql_spec: dict | None
    hl_keywords: list[str]
    ll_keywords: list[str]
    high_stakes: bool
    as_of: str | None
    degraded: list[str] = field(default_factory=list)  # ['router_degraded', 'nl2sql_downgraded', ...]


def fallback_route(query: str, as_of: str | None, reason: str) -> RoutedResult:
    """Safe fallback: rag-only when the router fails (never guess a route)."""
    hs = _has_high_stakes(query)
    return RoutedResult(
        rewritten=query,
        routing={
            "needs_rag": True,
            "needs_sql": False,
            "structured_path": "none",
            "high_stakes": hs,
            "needs_geo": _has_geo_intent(query),
        },
        sql_spec=None,
        hl_keywords=[],
        ll_keywords=[],
        high_stakes=hs,
        as_of=as_of,
        degraded=[reason],
    )


def _has_high_stakes(query: str) -> bool:
    """High-stakes keyword check — ORed with the LLM routing decision."""
    q = (query or "").lower()
    return any(k in q for k in HIGH_STAKES_KEYWORDS)


def _clean_json(text: str) -> str:
    """Strip a ```json fence and extract the first JSON object in the text."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        return t[start : end + 1]
    return t


def _is_budget_only_spec(spec: dict | None) -> bool:
    """A spec that is exactly one down-payment budget filter (fewshot Example-1 shape).

    Such a spec is the affordability intent itself — the deterministic leg may
    upgrade it. Anything else (inverted op, multi-filter, area/price/etc.) is a
    compound query we never hijack. Malformed filters (non-list, non-dict) are
    never budget-only and never crash.
    """
    if not isinstance(spec, dict):
        return False
    if spec.get("source") != "v_unit_offers":
        return False
    filters = spec.get("filters")
    if not isinstance(filters, list) or len(filters) != 1:
        return False
    f = filters[0]
    return (
        isinstance(f, dict)
        and f.get("field") == "required_down_payment_vnd"
        and f.get("op") in ("<", "<=")
    )


# Relational/compare price phrasings are NOT a budget — the force must not fire:
# 'trên X' / 'X trở lên' (above, inverted), 'từ X đến Y' (range), 'X và Y'
# (same-unit compare). 'dưới X' / 'đến X' (below/ceiling) stay budget-consistent
# ('affordable up to X') and are deliberately not suppressed, like 'hơn' (BH-1).
_RELATIONAL_PRICE_RE = re.compile(
    r"trên\s*[\d.,]+\s*(?:tỷ|tỉ|triệu|ngàn|nghìn)"
    r"|[\d.,]+\s*(?:tỷ|tỉ|triệu|ngàn|nghìn)\s*trở\s+lên"
    r"|(?:từ|giá\s+từ)\s*[\d.,]+\s*(?:tỷ|tỉ|triệu|ngàn|nghìn)\s*đến\s*[\d.,]+\s*(?:tỷ|tỉ|triệu|ngàn|nghìn)"
    r"|[\d.,]+\s*(?:tỷ|tỉ)\s+và\s+[\d.,]+\s*(?:tỷ|tỉ)"
    r"|[\d.,]+\s*triệu\s+và\s+[\d.,]+\s*triệu",
    re.IGNORECASE,
)


def _has_relational_price(query: str) -> bool:
    """True when a price literal is relational/compare, not a plain budget."""
    return bool(_RELATIONAL_PRICE_RE.search(query or ""))


def _normalize_routed(data: dict[str, Any], query: str, as_of: str | None) -> RoutedResult:
    """Coerce and constrain the LLM routing output to a canonical shape."""
    routing_raw = data.get("routing") or {}
    if isinstance(routing_raw, dict):
        needs_rag = bool(routing_raw.get("needs_rag", True))
        needs_sql = bool(routing_raw.get("needs_sql", False))
        path = str(routing_raw.get("structured_path", "none"))
    else:
        needs_rag, needs_sql, path = True, False, "none"

    if path not in ("spec", "nl2sql", "affordability", "none"):
        path = "none"

    needs_geo = _geo_flag(routing_raw, data, query)

    spec: dict | None = data.get("sql_spec")
    if not isinstance(spec, dict) or spec.get("source") not in ("facts", "v_unit_offers"):
        spec = None

    hl = data.get("hl_keywords") or []
    ll = data.get("ll_keywords") or []
    if isinstance(hl, str):
        hl = [hl]
    if isinstance(ll, str):
        ll = [ll]
    hl = [str(k) for k in hl if k]
    ll = [str(k) for k in ll if k]

    high_stakes = bool(data.get("high_stakes", False)) or _has_high_stakes(query)

    rewritten = str(data.get("rewritten_query") or query).strip()
    degraded: list[str] = []

    # Deterministic detector confirms NL2SQL (LLM proposes, detector decides).
    aggregate = detect_aggregate_intent(query) or detect_aggregate_intent(rewritten)
    if path == "nl2sql" and not aggregate:
        degraded.append("nl2sql_downgraded")
        if spec is not None:
            path = "spec"
        else:
            path, needs_rag, needs_sql = "none", True, False
    elif path != "nl2sql" and aggregate and needs_sql:
        degraded.append("nl2sql_forced")
        path = "nl2sql"

    if path == "nl2sql":
        needs_sql, needs_rag = True, needs_rag

    # Grounding safety net (§4.4): RAG always runs so a degraded/failed SQL spec
    # never leaves the answer without cited sources (SQL success still wins via
    # the merge; RAG-only is the designed fallback path).
    needs_rag = True

    # Affordability (ADR-0002 D2): a price-intent amount forces the deterministic
    # leg — LLM proposes, the detector confirms (same pattern as NL2SQL). The 1M
    # VND floor keeps counts/floors ('có 2 ngủ', 'tầng 10') out (FIX-3); compound
    # LLM specs, high-stakes/aggregate queries, and relational/compare phrasings
    # are never overridden. Budget takes the max of the declared 'có X' amount
    # and the bare price intent so compound budgets ('2 tỉ 500 triệu') keep the
    # full amount (extract_budget alone loses the follow-up — BH-6).
    budget = max(extract_budget(query) or 0, extract_price_intent(query) or 0) or None
    forced = False
    if (
        budget is not None
        and budget >= 1_000_000
        and path != "nl2sql"
        and not aggregate
        and not high_stakes
        and not _has_relational_price(query)
        and (spec is None or _is_budget_only_spec(spec))
    ):
        spec = {
            "subject_type": "unit",
            "source": "v_unit_estimates",
            "budget_vnd": budget,
            "limit": 20,
        }
        path = "affordability"
        needs_sql = True
        degraded.append("budget_injected")
        forced = True

    # LLM-declared 'affordability' the deterministic detector did NOT confirm
    # (no budget >= 1M, or a compound spec the leg cannot drive) must not reach
    # the deterministic leg — it would only degrade with spec_invalid. Demote to
    # the path the spec can honor: the LLM spec when present, else RAG-only
    # (VG-4/BH-3).
    if not forced and path == "affordability":
        degraded.append("affordability_unconfirmed")
        if spec is not None:
            path = "spec"
        else:
            path, needs_rag, needs_sql = "none", True, False

    return RoutedResult(
        rewritten=rewritten,
        routing={
            "needs_rag": needs_rag,
            "needs_sql": needs_sql,
            "structured_path": path,
            "high_stakes": high_stakes,  # shared by L4 guard, audit, and payload §16.2
            "needs_geo": needs_geo,  # maps leg — geo step self-gates; never blocks the pipeline
        },
        sql_spec=spec,
        hl_keywords=hl,
        ll_keywords=ll,
        high_stakes=high_stakes,
        as_of=as_of,
        degraded=degraded,
    )


def _truncate_history(history: list[dict] | None, max_turns: int = 4, max_chars: int = 3200) -> list[dict]:
    """History capped to 4 turns / ~3200 chars, dropping oldest turns first."""
    if not history:
        return []
    turns = [t for t in history if isinstance(t, dict) and t.get("role") in ("user", "assistant")][-max_turns:]
    total = 0
    kept: list[dict] = []
    for t in reversed(turns):
        chars = len(str(t.get("content", "")))
        if total + chars > max_chars and kept:
            break
        total += chars
        kept.append(t)
    return list(reversed(kept))


def _build_user_payload(query: str, history: list[dict] | None, as_of: str | None) -> str:
    hist_txt = "\n".join(
        f"{t['role']}: {t['content']}" for t in _truncate_history(history)
    ) or "(không có lịch sử)"
    return (
        f"{_FEWSHOT}\n\n"
        f"## Nhiệm vụ hiện tại\n"
        f"History (≤4 turn):\n{hist_txt}\n\n"
        f"Query: {query}\n"
        f"as_of (ISO date hoặc null): {as_of if as_of else 'null'}\n\n"
        "Trả về JSON duy nhất theo format các ví dụ trên."
    )


async def rewrite_query(query: str, history: list[dict] | None, as_of: str | None) -> RoutedResult:
    """One LLM JSON call: rewrite + route + spec, with one retry then rag-only fallback."""
    messages = [
        {"role": "system", "content": _SYSTEM_REWRITE},
        {"role": "user", "content": _build_user_payload(query, history, as_of)},
    ]
    model = model_for_role("rewrite")

    attempts = [messages, messages + [{"role": "user", "content": "JSON của bạn không hợp lệ. Hãy trả về đúng MỘT JSON object duy nhất, theo đúng schema các ví dụ."}]]
    for i, msgs in enumerate(attempts):
        try:
            text = await llm.complete(msgs, json_mode=True, model=model, timeout=LLM_CALL_TIMEOUT_S)
            data = json.loads(_clean_json(text))
            if not isinstance(data, dict):
                raise ValueError("LLM trả về không phải object")
            return _normalize_routed(data, query, as_of)
        except Exception as exc:  # noqa: BLE001 — json parse / LLM error -> retry then fallback
            logger.warning("rewrite attempt %d failed: %s", i + 1, exc)
            if i == len(attempts) - 1:
                return fallback_route(query, as_of, reason="router_degraded: rewrite LLM failed")

    # Should be unreachable — defensive parse edge.
    return fallback_route(query, as_of, reason="router_degraded")
