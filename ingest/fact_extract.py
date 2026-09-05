"""Fact extraction — LLM (qwen3.7-flash, JSON mode) and deterministic table extraction.

(plan §3.2 steps 3-4) One retry; invalid or low-confidence facts go to the review queue, never guessed.
(AD-14) Numeric discipline: money NUMERIC(20,0); pct NUMERIC(5,2); interest NUMERIC(6,4). No float.
"""  # noqa: E501

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from ingest.config import settings

# Schema extract
# 'm' (distance between towers/blocks) and 'count' (number of towers/units) are
# legitimately emitted by the extraction LLM; the DB CHECK (db/schema.sql facts
# table) must stay in lockstep with this tuple.
FACT_UNITS = ("vnd", "m2", "pct", "months", "days", "enum", "m", "count")
FACT_QUALITIES = ("exact", "range", "approx")
FACT_SUBJECT_TYPES = ("unit", "parcel", "project", "legal_fact", "taxon")

# LLM unit spellings -> canonical FACT_UNITS member. Unknown units degrade to
# 'enum' instead of failing validation: one sloppy unit must never wipe the
# facts of a whole document on re-ingest (08-24 wipe root cause).
_UNIT_ALIASES = {
    "m2": "m2",
    "m²": "m2",
    "met vuong": "m2",
    "mét vuông": "m2",
    "sqm": "m2",
    "m": "m",
    "meter": "m",
    "met": "m",
    "count": "count",
    "so luong": "count",
    "số lượng": "count",
    "pcs": "count",
    "vnd": "vnd",
    "đ": "vnd",
    "đồng": "vnd",
    "vnđ": "vnd",
    "dong": "vnd",
    "pct": "pct",
    "%": "pct",
    "phan tram": "pct",
    "phần trăm": "pct",
    "months": "months",
    "thang": "months",
    "tháng": "months",
    "month": "months",
    "days": "days",
    "ngay": "days",
    "ngày": "days",
    "day": "days",
    "dem": "days",
    "đêm": "days",
    "enum": "enum",
}


def _normalize_unit(u: str) -> str:
    """Map an LLM-emitted unit onto the canonical set, defaulting to 'enum'.

    Validation never guesses a fact's value — a unit we do not recognize is kept
    as 'enum' (with value_text intact) rather than dropping the fact.
    """
    key = (u or "").strip().lower()
    return _UNIT_ALIASES.get(key, "enum")


class ExtractedFact(BaseModel):
    """A single fact extracted from text or a table — Pydantic v2, validated at the boundary."""

    fact_key: str = Field(
        min_length=1
    )  # price_vnd | deposit_pct | term_months | interest_rate_pct | area_m2 | ...
    subject_key: str = Field(min_length=1)  # 'unit:tower-a/A10-01' | 'tax:le-phi-truoc-ba'
    subject_type: str = Field(default="")  # enforced in model_post_init after derivation
    subject_display: str = ""  # human-readable name
    value_num: Decimal | None = None
    value_text: str | None = None
    unit: str = Field(pattern="|".join(FACT_UNITS))
    quality: str = "exact"
    range_min: Decimal | None = None
    range_max: Decimal | None = None
    policy_key: str | None = None  # 'bank_a' | 'bank_b' | 'support'
    campaign_key: str | None = None
    extract_conf: float | None = None  # [0,1] — below 0.6 routes to the review queue
    span: str | None = None  # verbatim span in the source text

    def model_post_init(self, __context: Any) -> None:
        # The LLM may omit subject_type — derive it from the subject_key namespace
        # ('unit:camellia/studio' → 'unit', 'project:camellia' → 'project');
        # anything unrecognized falls back to 'taxon' instead of losing the fact.
        if not self.subject_type:
            prefix = self.subject_key.split(":", 1)[0] if ":" in self.subject_key else ""
            self.subject_type = prefix if prefix in FACT_SUBJECT_TYPES else "taxon"
        if self.subject_type not in FACT_SUBJECT_TYPES:
            raise ValueError(f"subject_type unknown: {self.subject_type!r}")
        # The LLM sometimes emits an aggregation/bound word ("max", "tối đa",
        # "khoảng") in `quality`; the facts_quality_check constraint only allows
        # exact|range|approx, so normalize here instead of losing the row.
        normalized = (self.quality or "").strip().lower()
        if normalized not in ("exact", "range", "approx"):
            if self.range_min is not None or self.range_max is not None:
                self.quality = "range"
            else:
                self.quality = "approx" if normalized in ("max", "min", "tối đa", "tối thiểu", "khoảng") else "exact"
        # Loose boundary checks: pct in [0,100]; vnd > 0 — fail at the input edge.
        if self.unit == "pct" and self.value_num is not None and not (0 <= self.value_num <= 100):
            raise ValueError(f"pct ngoài [0,100]: {self.value_num}")
        if self.unit == "vnd" and self.value_num is not None and self.value_num <= 0:
            raise ValueError(f"vnd phải > 0: {self.value_num}")


# Vietnamese number parsing (plan §4.2 + edge case 13/29).
_NUMBER_WORDS = {
    "không": 0,
    "một": 1,
    "hai": 2,
    "ba": 3,
    "bốn": 4,
    "năm": 5,
    "sáu": 6,
    "bảy": 7,
    "tám": 8,
    "chín": 9,
    "mười": 10,
    "mươi": 10,
    "trăm": 100,
    "nghìn": 1_000,
    "ngàn": 1_000,
    "triệu": 1_000_000,
    "tỷ": 1_000_000_000,
    "tỉ": 1_000_000_000,
}
_UNIT_WORDS = {
    "đồng": 1,
    "vnđ": 1,
    "đ": 1,
    "nghìn": 1_000,
    "ngàn": 1_000,
    "triệu": 1_000_000,
    "tỷ": 1_000_000_000,
    "tỉ": 1_000_000_000,
}

_AMOUNT_RE = re.compile(
    r"(?P<num>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*(?P<unit>tỷ|tỉ|triệu|nghìn|ngàn|đồng|vnđ|đ|m²|m2|%)?",
    re.IGNORECASE,
)


def _strip_thousands(s: str) -> str:
    # "2.850.000.000" → "2850000000"; "1,2" → "1.2" (a comma means a decimal separator when one digit follows)  # noqa: E501
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        return s.replace(".", "")
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", s):
        return s.replace(",", "")
    # "1,2" → "1.2" (Vietnamese decimals use a comma).
    if "," in s and "." not in s:
        return s.replace(",", ".")
    return s


def parse_vn_number(text: str) -> Decimal | None:
    """Parse a Vietnamese amount/area string to Decimal, or None when unrecognized.

    Accepts: "2,85 tỷ", "2.850.000.000đ", "85,5 m²", "25%", "một tỷ hai trăm".
    """
    t = text.strip().lower()
    if not t:
        return None

    # Number spelled out in words: "một tỷ hai trăm triệu" → 1_200_000_000
    words = t.split()
    if words and all(w in _NUMBER_WORDS or w in _UNIT_WORDS for w in words):
        total = Decimal(0)
        section = Decimal(0)
        number = Decimal(0)
        for w in words:
            if w in _NUMBER_WORDS:
                n = _NUMBER_WORDS[w]
                if n == 10 and section == 0:
                    number = 10
                elif n == 100:
                    number = (number or 1) * 100 if number else 100
                elif n in (1_000, 1_000_000, 1_000_000_000):
                    section += (number or 1) * n
                    number = 0
                else:
                    number = (number or 0) + n
            elif w in _UNIT_WORDS and _UNIT_WORDS[w] > 1:
                section += (number or 1) * _UNIT_WORDS[w]
                number = 0
        total = section + number
        if total > 0:
            return total

    m = _AMOUNT_RE.match(t)
    if not m:
        return None
    num_s = _strip_thousands(m.group("num"))
    try:
        num = Decimal(num_s)
    except InvalidOperation:
        return None
    unit = (m.group("unit") or "").lower()
    if unit == "%":
        return num  # percentage points, no scaling
    if unit == "m²" or unit == "m2":
        return num
    if unit in _UNIT_WORDS:
        num *= _UNIT_WORDS[unit]
    return num


def extract_amount(text: str) -> Decimal | None:
    """Return the first amount in text (for rewrite budget sizing)."""
    return parse_vn_number(text)


# Extraction LLM (JSON mode).
_EXTRACT_SYSTEM = (
    "Bạn trích xuất dữ liệu số liệu và chính sách từ tài liệu bất động sản pháp lý Việt Nam. "
    "Trả về JSON array các fact. Chỉ tin vào nội dung văn bản — KHÔNG suy đoán. "
    "Nếu không có fact nào, trả []."
)

_EXTRACT_USER = """Trích xuất các fact (số liệu, diện tích, chính sách vay, giá) từ văn bản sau.
Quy tắc:
- fact_key: price_vnd | area_m2 | deposit_pct | term_months | interest_rate_pct | legal_status | ...
- unit: vnd | m2 | pct | months | days | enum | m | count
- value_num là số ĐÃ về đơn vị gốc (tiền = vnd nguyên, không 'tỷ').
- subject_key: 'unit:<project>/<mã-căn>' với bảng giá; 'tax:<slug>' cho thuế; 'project:<slug>' cho dự án.
- policy_key chỉ khi fact thuộc chính sách vay ngân hàng cụ thể ('bank_a', 'bank_b', 'support').
- quality: exact | range | approx (range kèm range_min/range_max).
- extract_conf: độ tự tin 0-1; <0.6 là không chắc (sẽ vào review queue).
- span: đoạn gốc chứa fact.

Văn bản:
---
{text}
---
"""  # noqa: E501


def _strip_code_fence(raw: str) -> str:
    """Remove a markdown ``` fence around a JSON payload (deepseek-v4 habit)."""
    text = raw.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        text = text[first_nl + 1:] if first_nl != -1 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _salvage_truncated_array(text: str) -> list | None:
    """Parse the completed records of a length-truncated JSON array.

    Dense project sheets overflow the provider output cap mid-record; the
    finished objects before the cut are valid facts — salvage them instead of
    discarding the whole document (returns None when nothing is salvageable).
    """
    t = text.strip()
    if not t.startswith("["):
        return None
    last = t.rfind("},")
    if last == -1:
        return None
    try:
        parsed = json.loads(t[: last + 1] + "]")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


async def extract_facts(text: str, doc_id: str, kind: str) -> list[ExtractedFact]:
    """Extract facts via LLM JSON mode (one retry); raise on failure so load.py records the review queue."""  # noqa: E501
    import openai

    client = openai.AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url_v1,
        timeout=settings.extract_timeout,
    )
    payload = _EXTRACT_USER.format(text=text[:12000])

    last_err: Exception | None = None
    for _attempt in range(2):
        try:
            # Reasoning-capable extract models (deepseek-v4-*) burn the whole
            # max_tokens budget on reasoning_content and return empty content,
            # so thinking is disabled up front; providers that reject the field
            # fall back to a plain call (2026-08-27 extraction-drift repair).
            kwargs: dict[str, Any] = dict(
                model=settings.llm_model_extract,
                messages=[
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": payload},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
                # Dense registry docs (project sheets) emit hundreds of fact
                # lines; 4096 truncates the JSON array mid-record (2026-08-27).
                max_tokens=16384,
            )
            try:
                resp = await client.chat.completions.create(
                    **kwargs, extra_body={"thinking": {"type": "disabled"}}
                )
            except openai.BadRequestError:
                resp = await client.chat.completions.create(**kwargs)
            raw = resp.choices[0].message.content or "[]"
            text = _strip_code_fence(raw)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = _salvage_truncated_array(text)
                if data is None:
                    raise
            if isinstance(data, dict):
                data = data.get("facts", data.get("data", []))
            # Normalize LLM units onto the canonical set BEFORE validation — a
            # spelling variant like 'm²' or an unknown unit must degrade to the
            # closest canonical unit (or 'enum'), never abort the whole doc.
            for f in data:
                if isinstance(f, dict) and f.get("unit"):
                    f["unit"] = _normalize_unit(str(f["unit"]))
            return [ExtractedFact.model_validate(f) for f in data]
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

    raise RuntimeError(f"extract_facts lỗi (doc={doc_id}): {last_err}")


def extract_facts_from_table(
    header: list[str], rows: list[list[str]], campaign_key: str | None = None
) -> list[ExtractedFact]:
    """Deterministic table extraction: price_vnd/area_m2/deposit_pct/term_months/interest_rate_pct.

    Column headers are matched by keyword ('giá'/'price', 'diện tích'/'area', 'trả trước'/'cọc', ...).
    """  # noqa: E501
    facts: list[ExtractedFact] = []
    idx_price = _find_col(header, ("giá", "price", "value"))
    idx_area = _find_col(header, ("diện tích", "dien tich", "area", "dt"))
    idx_dep = _find_col(header, ("trả trước", "tra truoc", "cọc", "deposit"))
    idx_term = _find_col(header, ("thời hạn", "thoi han", "kỳ hạn", "ky han", "term"))
    idx_intr = _find_col(header, ("lãi suất", "lai suat", "interest"))
    idx_subj = _find_col(header, ("mã căn", "ma can", "căn", "can", "unit", "subject"))

    for row in rows:
        subj = row[idx_subj].strip() if idx_subj is not None and idx_subj < len(row) else None
        if not subj:
            continue
        subject_key = f"unit:{_slug(subj)}"
        if idx_price is not None and idx_price < len(row):
            v = parse_vn_number(row[idx_price])
            if v is not None:
                facts.append(
                    ExtractedFact(
                        fact_key="price_vnd",
                        subject_key=subject_key,
                        subject_type="unit",
                        subject_display=subj,
                        value_num=v,
                        unit="vnd",
                        campaign_key=campaign_key,
                    )
                )
        if idx_area is not None and idx_area < len(row):
            v = parse_vn_number(row[idx_area])
            if v is not None:
                facts.append(
                    ExtractedFact(
                        fact_key="area_m2",
                        subject_key=subject_key,
                        subject_type="unit",
                        subject_display=subj,
                        value_num=v,
                        unit="m2",
                        campaign_key=campaign_key,
                    )
                )
        if idx_dep is not None and idx_dep < len(row):
            v = parse_vn_number(row[idx_dep])
            if v is not None:
                facts.append(
                    ExtractedFact(
                        fact_key="deposit_pct",
                        subject_key=subject_key,
                        subject_type="unit",
                        subject_display=subj,
                        value_num=v,
                        unit="pct",
                        campaign_key=campaign_key,
                    )
                )
        if idx_term is not None and idx_term < len(row):
            v = parse_vn_number(row[idx_term])
            if v is not None:
                facts.append(
                    ExtractedFact(
                        fact_key="term_months",
                        subject_key=subject_key,
                        subject_type="unit",
                        subject_display=subj,
                        value_num=v,
                        unit="months",
                        campaign_key=campaign_key,
                    )
                )
        if idx_intr is not None and idx_intr < len(row):
            v = parse_vn_number(row[idx_intr])
            if v is not None:
                facts.append(
                    ExtractedFact(
                        fact_key="interest_rate_pct",
                        subject_key=subject_key,
                        subject_type="unit",
                        subject_display=subj,
                        value_num=v,
                        unit="pct",
                        campaign_key=campaign_key,
                    )
                )
    return facts


def _find_col(header: list[str], keys: tuple[str, ...]) -> int | None:
    for i, h in enumerate(header):
        hl = h.lower()
        if any(k in hl for k in keys):
            return i
    return None


def _slug(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
