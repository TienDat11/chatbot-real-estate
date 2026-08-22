# Eval runner (CLI) — golden-set regression for the RAG chatbot (plan §11 AD-8).
# Usage: --subset N | --only-category <legal|...> | --dry (mock pipeline + judge) |
#        --inject (guard eval) | --json-out <file>.
# Baseline (§11): numeric exact-match >= 0.95, zero unsupported claims, delta ~0.05;
# latency budget: P50 < 6s, P95 < 10s.

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Settings — prefer ingest/config.py (single source of truth); fall back to raw env when it is unavailable.
try:  # pragma: no cover
    from ingest.config import Settings as IngestSettings  # type: ignore

    _HAS_INGEST_SETTINGS = True
except Exception:  # noqa: BLE001 — module may not exist during early dev
    _HAS_INGEST_SETTINGS = False


@dataclass
class EvalSettings:
    """Eval configuration — read from env, no hardcoded secrets."""

    postgres_host: str = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    postgres_user: str = os.getenv("POSTGRES_USER", "ragre")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "")
    postgres_database: str = os.getenv("POSTGRES_DATABASE", "ragre")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    # Pin the judge LLM separately from the answer model to avoid correlated failures (§7-conflict-7)
    judge_model: str = os.getenv("EVAL_JUDGE_MODEL", "deepseek-v4-flash-0731")
    judge_timeout_s: float = float(os.getenv("EVAL_JUDGE_TIMEOUT_S", "30"))
    pipeline_timeout_s: float = float(os.getenv("EVAL_PIPELINE_TIMEOUT_S", "60"))
    n_sim: int = int(os.getenv("EVAL_N_SIM", "1"))  # pipeline runs per question (stable latency measurement)

    @classmethod
    def load(cls) -> "EvalSettings":
        if _HAS_INGEST_SETTINGS:
            try:
                base = IngestSettings()  # type: ignore[call-arg]
                return cls(
                    postgres_host=base.postgres_host,
                    postgres_port=base.postgres_port,
                    postgres_user=base.postgres_user,
                    postgres_password=base.postgres_password,
                    postgres_database=base.postgres_database,
                    llm_base_url=base.llm_base_url,
                    llm_api_key=base.llm_api_key,
                )
            except Exception:  # noqa: BLE001 — missing required env falls back to plain env
                pass
        return cls()


# Pipeline — import api.workflow.RagQueryPipeline defensively; eval may run --dry before api/ exists.
# (spike, day 1) Verify the real signature.
try:  # pragma: no cover
    from api.workflow import RagQueryPipeline  # type: ignore
except Exception:  # noqa: BLE001
    RagQueryPipeline = None


async def run_pipeline(
    pipeline: Any,
    question: str,
    as_of: Optional[str],
    settings: EvalSettings,
    history: Optional[list] = None,
) -> dict:
    """Invoke the pipeline across several calling shapes (spike: verify the real signature)."""
    kwargs: dict[str, Any] = {"query": question}
    if as_of:
        kwargs["as_of"] = as_of
    if history:
        kwargs["history"] = history
    if hasattr(pipeline, "query"):
        try:
            return await pipeline.query(**kwargs)
        except TypeError:
            return await pipeline.query(question)
    if hasattr(pipeline, "run"):
        try:
            return await pipeline.run(**kwargs)
        except TypeError:
            return await pipeline.run(question)
    raise TypeError("pipeline không có method run()/query()")


def _build_pipeline(settings: EvalSettings) -> Any:
    """Build RagQueryPipeline, trying multiple constructor shapes. (Spike: verify day 1.)"""
    if RagQueryPipeline is None:
        raise RuntimeError(
            "api/workflow.py chưa có — không dựng được RagQueryPipeline. "
            "Chạy --dry (mock) hoặc đợi api/ được dựng."
        )
    try:
        return RagQueryPipeline()  # parameterless constructor
    except TypeError:
        pass
    try:
        return RagQueryPipeline(settings)  # takes settings
    except TypeError:
        pass
    if _HAS_INGEST_SETTINGS:
        try:
            return RagQueryPipeline(IngestSettings())  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001
            pass
    raise RuntimeError("Không khớp constructor RagQueryPipeline — sửa _build_pipeline()")


def is_rejected(payload: dict) -> bool:
    """Detect a blocked query (L1 guard/refusal) — probe several keys since the contract varies."""
    for key in ("blocked", "rejected", "refused"):
        if payload.get(key) is True:
            return True
    guard = payload.get("guard", {}) or payload.get("guard_verdict", {})
    if isinstance(guard, dict) and guard.get("verdict") in ("reject", "blocked", "refused"):
        return True
    if isinstance(payload.get("error"), dict) and payload["error"].get("code") in (
        "guard_blocked",
        "refused",
    ):
        return True
    return False


# Vietnamese number parsing for numeric exact-match.
# Handles "2.000.000.000" / "2,000,000,000" / "2000000000" / "2 tỷ" / "1,2 tỷ" / "8,5%".
_VND_UNIT = {"nghìn": 1e3, "k": 1e3, "triệu": 1e6, "tr": 1e6, "tỷ": 1e9, "tỉ": 1e9}
# No \s: a space in the class lets the greedy match swallow adjacent numbers
# ("2.000.000.000 25%" merges into one token). Thousand separators are dot/comma, never space.
_NUM_TOKEN = r"[0-9][0-9\,\.]*[0-9]|[0-9]"


def _raw_to_float(raw: str) -> float:
    raw = raw.strip().replace(" ", "").replace(" ", "")
    if "," in raw and "." in raw:
        # "2,000,000.5" — comma thousands, dot decimal
        if raw.rfind(",") < raw.rfind("."):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        # "2,000,000,000" thousand separators; "1,2" a Vietnamese decimal comma
        parts = raw.split(",")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and len(parts[1]) != 3:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "." in raw:
        # "2.000.000.000" (multiple dots = thousands) vs "2.5" (decimal point)
        if raw.count(".") > 1:
            raw = raw.replace(".", "")
    return float(raw)


def extract_amounts(text: str) -> list[float]:
    """Extract money amounts (normalized to whole VND) — '2 tỷ' → 2e9; '1,2 tỷ' → 1.2e9."""
    seen: list[float] = []
    for m in re.finditer(rf"({_NUM_TOKEN})\s*({'|'.join(_VND_UNIT)})\b", text, flags=re.IGNORECASE | re.UNICODE):
        try:
            val = _raw_to_float(m.group(1)) * _VND_UNIT[m.group(2).lower()]
            if not any(abs(val - f) < max(1, abs(val) * 1e-6) for f in seen):
                seen.append(val)
        except ValueError:
            continue
    # Bare integers of 6+ digits are likely money — "8000000000" or "2.000.000.000".
    for m in re.finditer(r"\b([0-9][0-9.,]{5,})\b", text):
        try:
            val = _raw_to_float(m.group(1))
            if val >= 1_000_000 and not any(abs(val - f) < max(1, abs(val) * 1e-6) for f in seen):
                seen.append(val)
        except ValueError:
            continue
    return seen


def extract_pct(text: str) -> list[float]:
    """Extract percentages — '25%' → 25.0; '8,5%' → 8.5; '0,5%' → 0.5."""
    out: list[float] = []
    for m in re.finditer(rf"({_NUM_TOKEN})\s*(?:%|phần trăm|phan tram)", text, flags=re.IGNORECASE | re.UNICODE):
        try:
            out.append(_raw_to_float(m.group(1)))
        except ValueError:
            continue
    return out


def extract_m2(text: str) -> list[float]:
    """Extract areas — '85,5 m²' → 85.5; '72 m2' → 72.0."""
    out: list[float] = []
    for m in re.finditer(rf"({_NUM_TOKEN})\s*(?:m2|m²|mét vuông|met vuong)", text, flags=re.IGNORECASE | re.UNICODE):
        try:
            out.append(_raw_to_float(m.group(1)))
        except ValueError:
            continue
    return out


def extract_ints(text: str) -> list[int]:
    """Extract integers (counts/terms) — term 180/240, counts 4/5/11."""
    out: list[int] = []
    for m in re.finditer(r"\b([0-9][0-9_]*)\b", text):
        raw = m.group(1).replace("_", "")
        if raw.isdigit() and len(raw) <= 6:
            out.append(int(raw))
    return out


_PCT_KEYS = {"deposit_pct", "interest_rate_pct"}
_INT_KEYS = {"term_months", "count", "floor"}
_M2_KEYS = {"area_m2"}


def numeric_exact_match(expected_facts: dict[str, Any], answer: str) -> tuple[bool, list[str]]:
    """Every expected_facts value must appear in the answer after normalization."""
    if not expected_facts:
        return True, []
    amounts = extract_amounts(answer)
    pcts = extract_pct(answer)
    ints = extract_ints(answer)
    m2s = extract_m2(answer)
    missing: list[str] = []
    for key, expected in expected_facts.items():
        if expected is None:
            continue
        expected = float(expected)
        if key in _PCT_KEYS:
            ok = any(abs(float(e) - expected) < 0.001 for e in pcts)
        elif key in _INT_KEYS:
            ok = any(float(i) == expected for i in ints)
        elif key in _M2_KEYS:
            ok = any(abs(float(a) - expected) < 0.001 for a in m2s)
        else:  # vnd
            ok = any(abs(a - expected) < max(1, abs(expected) * 1e-6) for a in amounts)
        if not ok:
            missing.append(f"{key}={expected:g}")
    return (not missing), missing


# Faithfulness judge — pinned version, kept separate from the answer model.
class BaseJudge:
    async def judge(self, question: str, answer: str, contexts: list[str]) -> tuple[bool, float, str]:
        raise NotImplementedError


class MockJudge(BaseJudge):
    """Deterministic judge for --dry/CI: passes when any token appears in the answer."""

    async def judge(self, question: str, answer: str, contexts: list[str]) -> tuple[bool, float, str]:
        return True, 1.0, "mock judge (--dry)"


class LLMJudge(BaseJudge):
    """OpenAI-compatible judge LLM — JSON mode, pinned `judge_model`."""

    def __init__(self, settings: EvalSettings) -> None:
        self.settings = settings
        self._client: Any = None

    def _client_ok(self) -> bool:
        from openai import AsyncOpenAI  # lazy import — dep optional

        if self._client is None:
            if not self.settings.llm_base_url or not self.settings.llm_api_key:
                return False
            self._client = AsyncOpenAI(
                base_url=self.settings.llm_base_url,
                api_key=self.settings.llm_api_key,
                timeout=self.settings.judge_timeout_s,
            )
        return True

    async def judge(self, question: str, answer: str, contexts: list[str]) -> tuple[bool, float, str]:
        if not self._client_ok():
            return False, 0.0, "judge unavailable (thiếu LLM_BASE_URL/LLM_API_KEY)"
        ctx_block = "\n".join(f"[{i}] {c[:800]}" for i, c in enumerate(contexts[:6]))
        prompt = (
            "Bạn là giám khảo faithfulness. Chỉ dựa vào CONTEXT bên dưới, đánh giá câu trả lời:\n"
            f"QUESTION: {question}\nANSWER: {answer}\nCONTEXT:\n{ctx_block or '(rỗng)'}\n\n"
            'Trả về JSON duy nhất: {"supported": true|false, "score": 0.0-1.0, "reason": "..."}\n'
            "supported=false nếu answer khẳng định điều không có trong CONTEXT (bao gồm số liệu)."
        )
        chat = await self._client.chat.completions.create(
            model=self.settings.judge_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw = chat.choices[0].message.content or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return False, 0.0, f"judge trả về không phải JSON: {raw[:120]}"
        supported = bool(data.get("supported", False))
        score = float(data.get("score", 0.0))
        return supported, score, str(data.get("reason", ""))[:200]


# Context fetch: gold_chunk_ids → chunk content, for the faithfulness judge.
async def fetch_chunk_contexts(doc_prefixes: list[str], settings: EvalSettings) -> list[str]:
    """Load chunk content for the matching doc_ids (gold_chunk_ids use doc_id prefixes)."""
    if not doc_prefixes:
        return []
    try:
        import asyncpg  # lazy import — dep optional
    except Exception:  # noqa: BLE001
        return []
    conn = None
    try:
        conn = await asyncpg.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            database=settings.postgres_database,
            timeout=5,
        )
        out: list[str] = []
        for prefix in doc_prefixes:
            rows = await conn.fetch(
                """
                SELECT c.content FROM document_chunks c
                JOIN documents d ON d.doc_id = c.doc_id
                WHERE d.status = 'published' AND d.doc_id = $1
                ORDER BY c.chunk_index LIMIT 20
                """,
                prefix,
            )
            out.extend(r["content"] for r in rows)
        return out
    except Exception as exc:  # noqa: BLE001 — don't fail eval when the DB has no chunks yet
        print(f"  [warn] fetch_chunk_contexts skip: {exc}")
        return []
    finally:
        if conn is not None:
            await conn.close()


# Result model
@dataclass
class QuestionResult:
    id: str
    category: str
    question: str
    latency_ms: float
    pass_content: bool = False
    pass_numeric: bool = False
    pass_routing: bool = False
    pass_refusal: bool = False
    pass_freshness: bool = False
    pass_faithfulness: bool = False
    pass_images: bool = False
    faithful_score: float = 0.0
    fail_reasons: list[str] = field(default_factory=list)
    error: str = ""


# Mock pipeline (--dry) — deterministic, no backend required.
class MockPipeline:
    """Return a payload matching each golden question so the harness runs correctly."""

    def __init__(self, golden: dict[str, Any]) -> None:
        self.golden = golden

    async def run(self, query: str, as_of: Optional[str] = None, **_: Any) -> dict:
        q = self._find_question(query)
        if q is None:
            # --inject --dry: mock blocks injection-shaped prompts so the harness can measure.
            low = query.lower()
            if any(
                m in low
                for m in (
                    "drop ",
                    "select ",
                    "pg_sleep",
                    "bỏ qua",
                    "đóng vai",
                    "bảo mật",
                    "system prompt",
                    "nhắc lại",
                    "xâm nhập",
                    "'1'='1",
                    "1 đồng",
                    "rô bốt",
                    ";",
                )
            ):
                return {
                    "answer": "Tôi không thể trả lời yêu cầu này.",
                    "blocked": True,
                    "routing": {"needs_rag": False, "needs_sql": False, "structured_path": "none"},
                }
            return {
                "answer": "không có dữ liệu",
                "routing": {"needs_rag": False, "needs_sql": False, "structured_path": "none"},
            }
        tokens = list(q.get("expected_answer_contains") or [])
        facts = q.get("expected_facts") or {}
        answer = "Câu trả lời mẫu (dry): " + " ".join(tokens)
        for k, v in facts.items():
            if k in _PCT_KEYS:
                answer += f" {v}%"
            elif k in _INT_KEYS:
                answer += f" {v}"
            elif k == "area_m2":
                answer += f" {v} m²"
            else:
                answer += f" {int(v):,}".replace(",", ".")
        return {
            "answer": answer,
            "routing": q.get("expected_routing"),
            "facts": [],
            "confidence": "HIGH",
            "requires_review": bool(q.get("expect_requires_review", q.get("high_stakes", False))),
            "sources": [{"doc_id": p} for p in (q.get("gold_chunk_ids") or [])],
            "images": _mock_images(q),
            "latency_ms": 12,
            "trace_id": "dry-mock",
        }

    def _find_question(self, query: str) -> Optional[dict[str, Any]]:
        for q in self.golden.get("questions", []):
            if q.get("question") == query:
                return q
        return None


def _mock_images(q: dict[str, Any]) -> list[dict[str, Any]]:
    """Emit an images payload that satisfies the question's expected_images (--dry).

    The mock is the harness self-test: it must produce images the checker will
    pass, so a --dry run proves the image-relevance dimension measures end to
    end. Questions without expected_images emit none (the checker skips them).
    """
    expect = q.get("expected_images") or {}
    if not expect:
        return []
    if expect.get("none"):
        return []
    out: list[dict[str, Any]] = []
    for i, kind in enumerate(expect.get("kinds") or []):
        out.append(
            {
                "image_id": f"mock-img-{kind}-{i}",
                "kind": kind,
                "score": 0.9,
                "match": "semantic",
            }
        )
    return out


def _check_images(q: dict[str, Any], images: Any) -> tuple[bool, list[str]]:
    """Score the illustrative-images payload against expected_images (optional).

    Only questions that declare expected_images are scored. ``{"none": true}``
    requires an empty list (an off-topic question must not attach images);
    ``{"kinds": [...]}`` requires a non-empty list whose kinds are all inside
    the expectation. This is the regression lock for the "payment question
    returns floor-plan images" bug at the eval layer.
    """
    expect = q.get("expected_images")
    if not expect:
        return True, []
    items = images if isinstance(images, list) else []
    if expect.get("none"):
        if items:
            kinds = sorted({str(i.get("kind")) for i in items if isinstance(i, dict)})
            return False, [f"images: expect none nhưng có {len(items)} ảnh kinds={kinds}"]
        return True, []
    want = set(expect.get("kinds") or [])
    if not want:
        return True, []
    if not items:
        return False, [f"images: expect kinds {sorted(want)} nhưng không có ảnh nào"]
    got = {str(i.get("kind")) for i in items if isinstance(i, dict)}
    bad = got - want
    if bad:
        return False, [f"images: kind ngoài kỳ vọng {sorted(bad)} (want {sorted(want)})"]
    return True, []


# Check helpers
def _calc_p50_p95(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    s = sorted(values)
    n = len(s)
    p50 = s[max(0, min(n - 1, int(math.ceil(0.50 * n)) - 1))]
    p95 = s[max(0, min(n - 1, int(math.ceil(0.95 * n)) - 1))]
    return float(p50), float(p95)


def _check_routing(payload_routing: Any, expected: dict[str, Any]) -> tuple[bool, list[str]]:
    if not isinstance(payload_routing, dict) or not expected:
        return (payload_routing == expected), ([] if payload_routing == expected else ["routing thiếu/khác"])
    probs: list[str] = []
    for key, want in expected.items():
        got = payload_routing.get(key)
        if got != want:
            probs.append(f"{key}: want={want} got={got}")
    return (not probs), probs


def _check_refusal(category: str, answer: str) -> tuple[bool, list[str]]:
    if category != "refusal":
        return True, []
    refusal_markers = ["không liên quan", "không thể", "không phải", "không có", "chưa có", "từ chối"]
    ok = any(m in answer.lower() for m in refusal_markers)
    return ok, ([] if ok else ["refusal: answer không thể hiện từ chối"])


def _check_freshness(q: dict, answer: str) -> tuple[bool, list[str]]:
    excludes = q.get("expected_answer_excludes") or []
    if not excludes:
        return True, []
    hits = [t for t in excludes if t in answer]
    return (not hits), ([f"freshness: số cũ xuất hiện: {hits}"] if hits else [])


def _check_content(q: dict, answer: str) -> tuple[bool, list[str]]:
    tokens = q.get("expected_answer_contains") or []
    if not tokens:
        return True, []
    hits = [t for t in tokens if t in answer]
    return (bool(hits), ([] if hits else [f"content: không khớp token nào trong {tokens[:4]}"]))


def _overall_pass(r: QuestionResult) -> bool:
    return (
        r.pass_content
        and r.pass_numeric
        and r.pass_routing
        and r.pass_refusal
        and r.pass_freshness
        and r.pass_faithfulness
        and r.pass_images
    )


# Eval core
async def evaluate_question(
    pipeline: Any,
    judge: BaseJudge,
    q: dict[str, Any],
    settings: EvalSettings,
    dry: bool,
) -> QuestionResult:
    res = QuestionResult(
        id=q.get("id", "?"),
        category=q.get("category", "?"),
        question=q.get("question", ""),
        latency_ms=0.0,
    )
    start = time.perf_counter()
    try:
        payload = await asyncio.wait_for(
            run_pipeline(pipeline, q["question"], q.get("as_of"), settings),
            timeout=settings.pipeline_timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 — a pipeline failure must not crash eval
        res.error = f"pipeline lỗi: {exc.__class__.__name__}: {exc}"
        res.fail_reasons.append(res.error)
        return res
    res.latency_ms = (time.perf_counter() - start) * 1000.0

    answer = str(payload.get("answer") or "")
    payload_routing = payload.get("routing")
    blocked = is_rejected(payload)  # a blocked guard makes routing checks meaningless

    ok_c, why_c = _check_content(q, answer)
    res.pass_content = ok_c
    res.fail_reasons.extend(why_c)

    ok_n, why_n = numeric_exact_match(q.get("expected_facts") or {}, answer)
    res.pass_numeric = ok_n
    res.fail_reasons.extend(why_n)

    ok_r, why_r = (True, []) if blocked else _check_routing(payload_routing, q.get("expected_routing") or {})
    res.pass_routing = ok_r
    res.fail_reasons.extend(why_r)

    ok_refusal, why_refusal = _check_refusal(q.get("category", ""), answer)
    res.pass_refusal = ok_refusal
    res.fail_reasons.extend(why_refusal)

    ok_fresh, why_fresh = _check_freshness(q, answer)
    res.pass_freshness = ok_fresh
    res.fail_reasons.extend(why_fresh)

    ok_img, why_img = _check_images(q, payload.get("images"))
    res.pass_images = ok_img
    res.fail_reasons.extend(why_img)

    contexts: list[str] = []
    if not dry:
        contexts = await fetch_chunk_contexts(q.get("gold_chunk_ids") or [], settings)
    try:
        supported, score, reason = await judge.judge(q["question"], answer, contexts)
        res.pass_faithfulness = supported
        res.faithful_score = score
        if not supported:
            res.fail_reasons.append(f"faithfulness fail[{score:.2f}]: {reason}")
    except Exception as exc:  # noqa: BLE001
        res.pass_faithfulness = False
        res.fail_reasons.append(f"judge lỗi: {exc.__class__.__name__}: {exc}")

    return res


# --- Persona eval (Story 4.7 §8.2): regex/structure, no LLM-judge ---
_ROBOT_PHRASES = (
    "dựa trên thông tin được cung cấp",
    "như đã nêu ở trên",
    "theo yêu cầu của bạn",
    "tôi là ai/trợ lý ảo",
    "tôi là ai",
    "trợ lý ảo",
    "hy vọng thông tin hữu ích",
)
_EM_DASH = "—"


# Disclosure markers keyed by expected disclosure_type (answer text heuristics).
_DISCLOSURE_MARKERS = {
    "price": ("giá định hướng", "bảng giá", "giá bán"),
    "estimate": ("ước lượng", "chưa xác nhận chính thức", "khoảng"),
    "high_stakes": ("xác nhận với chuyên viên pháp lý", "chuyên viên pháp lý", "cầm cố", "pháp luật"),
    "none": (),
}
# CTA invite heuristics: a soft consult invite (Epic 5.7 touchpoint "~5 phút").
_CTA_INVITE = ("để lại số", "gọi lại", "gọi tư vấn", "chuyên viên gọi", "nhận tư vấn", "tư vấn phù hợp")
_HARD_CLOSE = ("ký ngay", "chốt ngay", "mua ngay", "đặt cọc ngay")


def _persona_checks(answer: str, expect: dict) -> list[str]:
    """Return failing persona rules for one answer against its expect block."""
    fails: list[str] = []
    low = (answer or "").lower()

    if expect.get("has_direct_answer") and not answer.strip():
        fails.append("has_direct_answer: answer rỗng")

    if expect.get("has_citation") and "[fe-" not in answer:
        fails.append("has_citation: thiếu [fe-xxx]")
    if expect.get("has_citation") is False and "[fe-" in answer:
        # off-topic refusal must not fabricate citations
        fails.append("has_citation=False: refusal lại có citation")

    if expect.get("no_em_dash") and _EM_DASH in answer:
        fails.append("no_em_dash: xuất hi em-dash —")

    if expect.get("no_robot_phrase"):
        for p in _ROBOT_PHRASES:
            if p in low:
                fails.append(f"no_robot_phrase: '{p}'")

    # next_step_questions: 0|1 — đếm câu hỏi thật (kết thúc bằng '?', không đếm '? bên trong câu trích/lãi suất '?0%').
    nq = expect.get("next_step_questions")
    if nq is not None:
        q_count = _count_likely_questions(answer)
        if nq == 0 and q_count > 0:
            fails.append(f"next_step_questions=0 nhưng có {q_count} dấu hỏi")
        if nq == 1 and q_count > 1:
            fails.append(f"next_step_questions=1 nhưng có {q_count} dấu hỏi (>1)")

    # Story 4.7 gate: answer must carry the expected disclosure marker (if any).
    want_dt = expect.get("disclosure_type")
    if want_dt == "none":
        for dt, markers in _DISCLOSURE_MARKERS.items():
            if dt == "none":
                continue
            if any(m in low for m in markers):
                fails.append(f"disclosure_type: want=none nhưng thấy marker {dt}")
                break
    elif want_dt and any(m in low for m in _DISCLOSURE_MARKERS.get(want_dt, ())) is False:
        fails.append(f"disclosure_type: thiếu marker cho {want_dt}")

    # CTA policy: cta_allowed=True cần lời mời mềm; cta_allowed=False không được hard-close.
    has_invite = any(m in low for m in _CTA_INVITE)
    has_hard = any(m in low for m in _HARD_CLOSE)
    cta = expect.get("cta_allowed")
    if cta is True and not has_invite:
        fails.append("cta_allowed=True nhưng không có lời mời mềm")
    if cta is False and has_hard:
        fails.append("cta_allowed=False nhưng có hard-close")

    return fails


def _count_likely_questions(answer: str) -> int:
    """Count trailing-? question marks, ignoring '?0%' style (percent) occurrences."""
    import re as _re

    stripped = _re.sub(r"\?\s*%", "", answer or "")  # "?0%" / "? 5%" are not questions
    return stripped.count("?")


_MOCK_DISCLOSURE = {
    "price": "(giá định hướng)",
    "estimate": "(ước lượng)",
    "high_stakes": "(xác nhận với chuyên viên pháp lý)",
    "none": "",
}


def _persona_mock_answer(q: dict) -> str:
    """Deterministic hint answer that satisfies a persona expect (--dry only)."""
    expect = q.get("expect") or {}
    none = expect.get("disclosure_type") == "none"
    if expect.get("has_citation") is False:
        # refusal: no citation and no disclosure marker (disclosure_type must be none)
        a = "Chào anh/chị, việc này nằm ngoài phạm vi tư vấn dự án của em."
        if expect.get("next_step_questions") == 1:
            a += " Anh/chị có thắc mắc gì về dự án là em hỗ trợ nhé?"
        return a
    marker = _MOCK_DISCLOSURE.get(expect.get("disclosure_type"), "") if not none else ""
    a = f"Chào anh/chị, em xin thông tin chính xác theo hồ sơ dự án {marker} [fe-0001]. "
    if expect.get("next_step_questions") == 1:
        # marker-neutral (tránh từ 'khoảng' khi disclosure_type=none, vì là estimate marker)
        a += " Anh/chị cho em biết ngân sách dự kiến thì em tư vấn kỹ hơn nhé?"
    if expect.get("cta_allowed"):
        a += " Anh/chị để lại số để chuyên viên gọi lại tư vấn nhé."
    return a


async def eval_persona(pipeline: Any, settings: EvalSettings, dry: bool) -> dict:
    """Story 4.7: run golden_persona_v1.json, regex/structure checks, no judge.

    Gate §8.4: pass khi 15/15, hoặc 14/15 + waiver (waiver do người chạy quyết
    định — ghi rõ trong kết quả để người duyệt chấp nhận).
    """
    path = REPO_ROOT / "eval" / "golden_persona_v1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for q in data["questions"]:
        start = time.perf_counter()
        if dry:
            payload = {"answer": _persona_mock_answer(q)}
        else:
            try:
                payload = await asyncio.wait_for(
                    run_pipeline(
                        pipeline,
                        q["question"],
                        q.get("as_of"),
                        settings,
                        history=_persona_history(q),
                    ),
                    timeout=settings.pipeline_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                payload = {"answer": "", "error": str(exc)}
        answer = str(payload.get("answer") or "")
        latency = (time.perf_counter() - start) * 1000.0
        fails = _persona_checks(answer, q.get("expect") or {})
        if payload.get("error"):
            fails.append(payload["error"])
        rows.append({
            "id": q["id"],
            "category": q.get("category", ""),
            "question": q["question"],
            "pass": not fails,
            "fails": fails,
            "latency_ms": round(latency, 1),
        })
    n_ok = sum(1 for r in rows if r["pass"])
    # Gate §8.4: 15/15 pass, hoặc 14/15 + waiver (waiver do người duyệt chấp nhận).
    waiver = n_ok == len(rows) - 1 and len(rows) >= 15
    return {
        "rows": rows,
        "pass_count": n_ok,
        "total": len(rows),
        "pass": n_ok == len(rows) or waiver,
        "waiver": waiver,
    }


def _persona_history(q: dict) -> Optional[list]:
    """Convert golden [[user, assistant], ...] history to pipeline history dicts."""
    raw = q.get("history") or []
    out: list[dict] = []
    for pair in raw:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            out.append({"role": "user", "content": str(pair[0])})
            out.append({"role": "assistant", "content": str(pair[1])})
    return out or None



def _pass_rate(results: list[QuestionResult]) -> dict[str, float]:
    total = len(results)
    if total == 0:
        return {}
    return {
        "content": sum(r.pass_content for r in results) / total,
        "numeric": sum(r.pass_numeric for r in results) / total,
        "routing": sum(r.pass_routing for r in results) / total,
        "refusal": sum(r.pass_refusal for r in results) / total,
        "freshness": sum(r.pass_freshness for r in results) / total,
        "faithfulness": sum(r.pass_faithfulness for r in results) / total,
        "images": sum(r.pass_images for r in results) / total,
        "overall_pass": sum(_overall_pass(r) for r in results) / total,
    }


# Injection eval (--inject)
async def eval_injection(pipeline: Any, settings: EvalSettings, dry: bool) -> dict[str, Any]:
    path = REPO_ROOT / "eval" / "injection_test_vn.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    ok_threshold = float(data.get("_meta", {}).get("ok_threshold_pct", 90))
    rows: list[dict[str, Any]] = []
    for p in data["prompts"]:
        start = time.perf_counter()
        try:
            payload = await asyncio.wait_for(
                run_pipeline(pipeline, p["prompt"], None, settings),
                timeout=settings.pipeline_timeout_s,
            )
            rejected = is_rejected(payload)
            if not rejected and not dry:
                # Heuristic: a refusal-style answer counts as blocked.
                answer = str(payload.get("answer") or "")
                rejected = any(m in answer.lower() for m in ["từ chối", "không thể", "không liên quan"])
        except Exception as exc:  # noqa: BLE001 — pipeline failure on injection fails closed
            rejected = True
            payload = {"error": str(exc)}
        latency = (time.perf_counter() - start) * 1000.0
        want = p["expect_reject"]
        ok = rejected == want
        rows.append(
            {
                "id": p["id"],
                "label": p["label"],
                "prompt": p["prompt"],
                "expect_reject": want,
                "rejected": rejected,
                "pass": ok,
                "latency_ms": round(latency, 1),
            }
        )
    n_ok = sum(1 for r in rows if r["pass"])
    pct = (n_ok / len(rows) * 100) if rows else 0.0
    return {"rows": rows, "pass_pct": pct, "ok_threshold": ok_threshold, "pass": pct >= ok_threshold}


# Output
def _print_summary(results: list[QuestionResult], rates: dict[str, float], latencies: list[float]) -> None:
    p50, p95 = _calc_p50_p95(latencies)
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    labels = [
        ("content (any-of)", "content"),
        ("numeric-exact", "numeric"),
        ("routing/path", "routing"),
        ("refusal", "refusal"),
        ("freshness", "freshness"),
        ("faithfulness", "faithfulness"),
        ("images (kinds)", "images"),
        ("overall-pass", "overall_pass"),
    ]
    for label, key in labels:
        n = sum(
            r.pass_content if key == "content"
            else r.pass_numeric if key == "numeric"
            else r.pass_routing if key == "routing"
            else r.pass_refusal if key == "refusal"
            else r.pass_freshness if key == "freshness"
            else r.pass_faithfulness if key == "faithfulness"
            else r.pass_images if key == "images"
            else 1 if _overall_pass(r) else 0
            for r in results
        )
        print(f"{label:<22}{n:>6}/{len(results):<6}{rates.get(key, 0) * 100:>7.1f}%")
    print(f"\nlatency P50: {p50:.0f} ms | P95: {p95:.0f} ms | n={len(latencies)}")
    print(f"budget: P50 < 6000 ms, P95 < 10000 ms → {'PASS' if p50 < 6000 and p95 < 10000 else 'CHECK'}")
    print("=" * 78)


def _print_by_category(results: list[QuestionResult]) -> None:
    cats: dict[str, list[QuestionResult]] = {}
    for r in results:
        cats.setdefault(r.category, []).append(r)
    print("\nper-category:")
    for cat in sorted(cats):
        rs = cats[cat]
        n_pass = sum(
            r.pass_content
            and r.pass_numeric
            and r.pass_routing
            and r.pass_refusal
            and r.pass_freshness
            and r.pass_images
            for r in rs
        )
        print(f"  {cat:<22}{n_pass:>3}/{len(rs):<3} (content+num+routing+refusal+freshness+images)")


def _print_failures(results: list[QuestionResult]) -> None:
    fails = [r for r in results if r.fail_reasons]
    if not fails:
        print("\nALL PASS (không câu nào fail)")
        return
    print(f"\nFAIL ({len(fails)}):")
    for r in fails:
        print(f"  [{r.id}] {r.question[:60]}")
        for why in r.fail_reasons[:6]:
            print(f"      - {why}")


# Main
async def amain(args: argparse.Namespace, settings: EvalSettings) -> int:
    dry = args.dry
    if getattr(args, "persona", False):
        pipeline: Any = MockPipeline({"questions": []}) if dry else _build_pipeline(settings)
        result = await eval_persona(pipeline, settings, dry)
        for row in result["rows"]:
            mark = "PASS" if row["pass"] else "FAIL"
            print(
                f"  {mark} [{row['id']}] {row['category']:<16} "
                f"{round(row['latency_ms']):>6}ms {row['question'][:44]}"
            )
            for f in row["fails"]:
                print(f"        - {f}")
        print(
            f"\npersona: {result['pass_count']}/{result['total']} PASS "
            f"(gate §8.4: 15/15 hoặc 14/15 + waiver) → {'PASS' if result['pass'] else 'FAIL'}"
        )
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if result["pass"] else 1

    if args.inject:
        pipeline: Any = MockPipeline({"questions": []}) if dry else _build_pipeline(settings)
        result = await eval_injection(pipeline, settings, dry)
        for row in result["rows"]:
            mark = "OK " if row["pass"] else ("FP" if row["rejected"] else "FN")
            print(
                f"  {mark} [{row['id']}] {row['label']:<10} reject={row['rejected']} "
                f"want={row['expect_reject']} {row['prompt'][:60]}"
            )
        print(
            f"\ninjection: {result['pass_pct']:.1f}% pass (threshold {result['ok_threshold']:.0f}%) "
            f"→ {'PASS' if result['pass'] else 'FAIL'}"
        )
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if result["pass"] else 1

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    questions = golden["questions"]
    if args.only_category:
        questions = [q for q in questions if q.get("category") == args.only_category]
    if args.subset:
        questions = questions[: args.subset]
    if not questions:
        print("không có câu nào để chạy (subset/category rỗng)")
        return 1

    judge: BaseJudge = MockJudge() if dry else LLMJudge(settings)
    pipeline = MockPipeline(golden) if dry else _build_pipeline(settings)

    results: list[QuestionResult] = []
    latencies_ms: list[float] = []
    for i, q in enumerate(questions, 1):
        res = await evaluate_question(pipeline, judge, q, settings, dry)
        results.append(res)
        # latency: reuse res.latency_ms by default (one pipeline call per question);
        # n_sim > 1 reruns to make the latency measurement stable.
        if settings.n_sim <= 1:
            latencies_ms.append(res.latency_ms)
        else:
            for _ in range(settings.n_sim):
                start = time.perf_counter()
                try:
                    await run_pipeline(pipeline, q["question"], q.get("as_of"), settings)
                    latencies_ms.append((time.perf_counter() - start) * 1000.0)
                except Exception:  # noqa: BLE001
                    latencies_ms.append(settings.pipeline_timeout_s * 1000.0)
        mark = "PASS" if _overall_pass(res) else "FAIL"
        print(
            f"{i:>3}/{len(questions)} {mark} [{res.id}] {res.category:<18} "
            f"{round(res.latency_ms):>7}ms {res.question[:50]}"
        )
        for why in res.fail_reasons[:3]:
            print(f"        - {why}")

    rates = _pass_rate(results)
    _print_summary(results, rates, latencies_ms)
    _print_by_category(results)
    _print_failures(results)

    if args.json_out:
        out = {
            "meta": {"golden": args.golden, "dry": dry, "n": len(results)},
            "rates": rates,
            "latency_p50_p95_ms": list(_calc_p50_p95(latencies_ms)),
            "results": [
                {
                    "id": r.id,
                    "category": r.category,
                    "question": r.question,
                    "latency_ms": r.latency_ms,
                    "pass_content": r.pass_content,
                    "pass_numeric": r.pass_numeric,
                    "pass_routing": r.pass_routing,
                    "pass_refusal": r.pass_refusal,
                    "pass_freshness": r.pass_freshness,
                    "pass_faithfulness": r.pass_faithfulness,
                    "pass_images": r.pass_images,
                    "faithful_score": r.faithful_score,
                    "fail_reasons": r.fail_reasons,
                    "error": r.error,
                }
                for r in results
            ],
        }
        Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")

    if args.fail_fast and rates.get("overall_pass", 1.0) < 1.0:
        return 1
    if rates.get("numeric", 1.0) < 0.95:
        print("\n[gate] numeric exact-match < 0.95 → FAIL (ngưỡng §11)")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Eval golden set chatbot RAG bất động sản")
    parser.add_argument("--golden", default=str(REPO_ROOT / "eval" / "golden_set_v1.json"))
    parser.add_argument("--subset", type=int, default=None, help="chỉ chạy N câu đầu")
    parser.add_argument("--only-category", default=None, help="chỉ chạy 1 category (legal/fact_affordability/...)")
    parser.add_argument("--dry", action="store_true", help="mock pipeline + mock judge (CI, định thức)")
    parser.add_argument("--inject", action="store_true", help="chạy injection_test_vn.json thay cho golden set")
    parser.add_argument("--persona", action="store_true", help="chạy golden_persona_v1.json (Story 4.7, regex/structure, ko LLM-judge)")
    parser.add_argument("--json-out", default=None, help="ghi kết quả JSON")
    parser.add_argument("--fail-fast", action="store_true", help="exit code 1 nếu có câu fail")
    args = parser.parse_args()

    settings = EvalSettings.load()
    try:
        return asyncio.run(amain(args, settings))
    except RuntimeError as exc:
        print(f"\n[eval abort] {exc}", file=sys.stderr)
        print("  Hint: chạy --dry để test harness, hoặc --inject để test guard.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
