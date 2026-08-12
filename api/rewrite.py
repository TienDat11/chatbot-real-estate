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

# VN unit -> vnd multipliers (incl. variants "tỉ"/"ngàn"/"nghìn").
_VN_UNITS = {"tỷ": 1_000_000_000, "tỉ": 1_000_000_000, "triệu": 1_000_000, "ngàn": 1_000, "nghìn": 1_000}

_BUDGET_RE = re.compile(r"có\s+([\d.,]+)\s*(tỷ|tỉ|triệu|ngàn|nghìn)?\s*(?:tiền|đồng|vnd)?", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(\d[\d.,]*)\s*(tỷ|tỉ|triệu|ngàn|nghìn)?")

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


def parse_vn_number(text: str) -> int | None:
    """Parse a Vietnamese money literal: '2,85 tỷ' -> 2_850_000_000; plain digits -> int.

    Conventions: comma is the decimal separator, dot the thousand separator.
    """
    t = (text or "").strip().lower().replace(" ", " ")
    m = _NUMBER_RE.search(t)
    if not m:
        return None
    num_part, unit = m.group(1), (m.group(2) or "")
    # Strip thousand separators (dots), convert decimal comma to dot.
    if "," in num_part and "." in num_part:
        # "2.850.000,50" — dot is thousand, comma is decimal.
        num_part = num_part.replace(".", "").replace(",", ".")
    elif "," in num_part:
        num_part = num_part.replace(",", ".")  # "2,85" -> 2.85
    else:
        num_part = num_part.replace(".", "")  # "2.850.000.000" -> 2850000000
    try:
        value = float(num_part)
    except ValueError:
        return None
    value *= _VN_UNITS.get(unit, 1)
    return int(round(value))


def extract_budget(query: str) -> int | None:
    """Extract a declared budget ('tôi có 2 tỉ...') mapped to required_down_payment_vnd."""
    m = _BUDGET_RE.search((query or "").lower())
    if not m:
        return None
    raw = f"{m.group(1)} {m.group(2) or ''}".strip()
    return parse_vn_number(raw)


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


def _normalize_routed(data: dict[str, Any], query: str, as_of: str | None) -> RoutedResult:
    """Coerce and constrain the LLM routing output to a canonical shape."""
    routing_raw = data.get("routing") or {}
    if isinstance(routing_raw, dict):
        needs_rag = bool(routing_raw.get("needs_rag", True))
        needs_sql = bool(routing_raw.get("needs_sql", False))
        path = str(routing_raw.get("structured_path", "none"))
    else:
        needs_rag, needs_sql, path = True, False, "none"

    if path not in ("spec", "nl2sql", "none"):
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

    # Budget injection: declared budget without an LLM spec -> build an affordability spec.
    # Only for budget >= 1M VND (avoids "có 2 ngủ" being parsed as a 2 VND budget).
    if spec is None and needs_sql and path != "nl2sql":
        budget = extract_budget(query)
        if budget is not None and budget >= 1_000_000:
            spec = {
                "subject_type": "unit",
                "source": "v_unit_offers",
                "filters": [{"field": "required_down_payment_vnd", "op": "<=", "value": budget}],
                "order_by": {"field": "required_down_payment_vnd", "dir": "asc"},
                "limit": 10,
            }
            degraded.append("budget_injected")

    return RoutedResult(
        rewritten=rewritten,
        routing={
            "needs_rag": needs_rag,
            "needs_sql": needs_sql,
            "structured_path": path,
            "high_stakes": high_stakes,  # để L4 guard + audit + payload §16.2 dùng chung
            "needs_geo": needs_geo,  # maps leg — geo step tự gate, không chặn pipeline
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
