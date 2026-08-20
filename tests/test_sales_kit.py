"""Story 4.3 — sales kit anti-drift + conditional injection (plan §4.3/4.4).

Anti-drift: every number / percent / date written in the kit must exist in the
data/_processed source files the section header cites (JSON files + feedback
ground truth). Numbers without a source belong in the tail pending_confirm, not
in the kit. Injection: SALES_CONTEXT only reaches the message data block for
price/payment/project/handoff intents — legal lookup and off-topic stay clean.
"""

import json
import re
from pathlib import Path

from api.application.services.generate import build_messages
from api.application.services.merge import Merged
from api.application.services.sales_kit import (
    SALES_KIT_VERSION,
    inject_sales_context,
    sales_kit_block,
    _DELIMITER,
)
from api.domain.services.route_intent import Intent

_WS = Path(__file__).resolve().parents[1]
_KIT_PATH = _WS / "api" / "prompts" / "sales_kit_vn.md"
_DATA = _WS / "data" / "_processed"

# Source map: short ref -> (file glob, is_json)
_SOURCES = {
    "pj":  "project_info.json",
    "pm":  "payment_methods.json",
    "br":  "business_rules.json",
    "uc":  "unit_catalog.json",
    "pmx": "price_matrix.json",
    "gt":  "feed_back/feedback_data.txt",
}


def _kit() -> str:
    return _KIT_PATH.read_text(encoding="utf-8")


def _digits(text: str) -> str:
    """Digits only — used to compare numbers across formatting (2.297 vs 2297.0)."""
    return re.sub(r"\D", "", text or "")


def _source_text(refs: list[str]) -> str:
    parts: list[str] = []
    for r in refs:
        fn = _SOURCES.get(r)
        if not fn:
            continue
        path = _DATA / fn
        if not path.exists():
            continue
        parts.append(path.read_text(encoding="utf-8"))
    return " ".join(parts)


def test_kit_version_and_delimiter():
    assert SALES_KIT_VERSION == "SALES_KIT_V1"
    k = _kit()
    assert _DELIMITER in k.strip() or _DELIMITER in sales_kit_block()


def test_kit_token_budget():
    # Plan §4.2: kit <= 1200 token, CI counts via len // 3.5 fallback tokenizer.
    k = _kit()
    assert len(k) / 3.5 <= 1200, f"kit {len(k)} chars → {len(k)/3.5:.0f} tokens > 1200"


def test_kit_sections_present():
    k = _kit()
    for h in ("## A. USP", "## B. Benefit-translation", "## C. Payment selling angles",
              "## D. Objection playbook", "## E. FOMO template", "## Tail — pending_confirm"):
        assert h in k, f"missing kit section {h}"


def test_inject_condition_intents():
    assert inject_sales_context(Intent.PRICE) is True      # price/payment
    assert inject_sales_context(Intent.COMPANY) is True    # project/company
    assert inject_sales_context(Intent.HANDOFF) is True    # human contact
    assert inject_sales_context(Intent.LEGAL) is False     # legal lookup: clean
    assert inject_sales_context(Intent.OTHER) is False     # off-topic: clean
    assert inject_sales_context(Intent.LOCATION) is False  # maps leg: clean
    assert inject_sales_context(Intent.CLOSURE) is False


def _merged_for(query: str) -> Merged:
    return Merged(
        rag_blocks="RAG_CONTEXT\nđoạn văn bản pháp luật...",
        evidence_blocks="FACT_EVIDENCE\n[fe-001] giá 2.1 tỷ",
        sources=[],
        facts=[],
        meta={"query": query, "rewritten": query},
    )


def _data_block(msgs: list[dict]) -> str:
    """The user data message (RAG+evidence+SALES_CONTEXT) — NOT the system prompt,
    which itself mentions SALES_CONTEXT internally (story 4.2 voice rules)."""
    for m in msgs:
        if m.get("role") == "user" and "DỮ LIỆU THAM KHẢO" in (m.get("content") or ""):
            return m.get("content") or ""
    return ""


def test_build_messages_injects_for_price_query():
    msgs = build_messages(_merged_for("giá căn 2PN view biển bao nhiêu?"), None)
    db = _data_block(msgs)
    assert "SALES_CONTEXT" in db
    assert "The Camellia" in db or "Lê Văn Lương" in db
    assert "DỮ LIỆU THAM KHẢO" in db


def test_build_messages_injects_for_handoff_query():
    msgs = build_messages(_merged_for("cho em xin số hotline gọi tư vấn"), None)
    assert "SALES_CONTEXT" in _data_block(msgs)


def test_build_messages_skips_for_legal_lookup():
    msgs = build_messages(_merged_for("cầm cố đất có được luật ghi nhận không?"), None)
    assert "SALES_CONTEXT" not in _data_block(msgs)


def test_build_messages_skips_for_offtopic():
    msgs = build_messages(_merged_for("thời tiết hôm nay thế nào"), None)
    assert "SALES_CONTEXT" not in _data_block(msgs)


# Capture a numeric token plus an optional unit. The unit is NOT part of the
# digits ("2.297 m2" -> "2297", no trailing "2" leak).
_UNIT_RE = re.compile(r"(?:triệu|tỷ|đồng|đ|vnđ|%|m[²2]|tháng|năm|ngày)", re.IGNORECASE)
_NUM_RE = re.compile(r"\d[\d.,]*(?:\s*(?:triệu|tỷ|đồng|đ|vnđ|%|m[²2]|tháng|năm|ngày))?", re.IGNORECASE)


def _fact_digits(token: str) -> str:
    """Digits of the numeric core — units stripped first so "2.297 m2" -> "2297"
    (the trailing "2" of m2 must never leak into the comparison)."""
    return _digits(_UNIT_RE.sub("", token))


def _is_fact_number(token: str) -> bool:
    """Plan §4.3 scopes anti-drift to số tiền/%/ngày (+ dates + area): a token is
    checked only when it carries a money/percent/area/period unit, or is a
    >= 4-digit value (dates, years, amounts). Bare floors ("4/13/14") and unit
    counts ("81") are structural text, not data claims."""
    return bool(_UNIT_RE.search(token)) or len(_fact_digits(token)) >= 4



def test_antidrift_numbers_have_sources():
    """Every number/%/date in the kit must exist (digit-normalized) in a cited source."""
    k = _kit()
    failures: list[str] = []
    current_refs: list[str] = []
    src_pool: dict[str, str] = {}

    for line in k.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):  # header/legend comments carry no kit facts
            continue
        if line.startswith("## "):
            current_refs = re.findall(r"\b(pj|pm|br|uc|pmx|gt)\b", line)
            continue
        # gather refs mentioned anywhere in the line ([pj], Fact: pj, ->phap_ly with pj earlier)
        line_refs = current_refs + re.findall(r"\b(pj|pm|br|uc|pmx|gt)\b", line)
        for r in line_refs:
            if r not in src_pool:
                src_pool[r] = _source_text([r])
        numbers = _NUM_RE.findall(line)
        for n in numbers:
            if not n or not _is_fact_number(n):
                continue
            d = _fact_digits(n)
            hay = _digits(" ".join(src_pool.get(r, "") for r in current_refs + line_refs))
            if d not in hay:
                failures.append(f"{n!r} (in line: {line[:60]}) → not in {sorted(set(current_refs + line_refs))}")

    assert not failures, "anti-drift: numbers without a source:\n" + "\n".join(failures[:20])


def test_sales_context_block_is_nonempty():
    assert sales_kit_block().strip()
    assert "SALES_CONTEXT" in sales_kit_block()
