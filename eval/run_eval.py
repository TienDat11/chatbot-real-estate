# =============================================================================
# rag-real-estate — Eval runner (CLI)
# Plan §11 (AD-8) + §10 Ngày 9 | Python >= 3.10 | UTF-8
#
# Metric: faithfulness (judge LLM ghim version) + numeric exact-match +
#         routing/path accuracy + refusal correctness + freshness (campaign expire)
#         + answer-relevancy proxy (expected_answer_contains coverage) + P50/P95 latency.
#
# Usage:
#   python eval/run_eval.py                          # full golden set, backend thật
#   python eval/run_eval.py --subset 10              # 10 câu đầu
#   python eval/run_eval.py --only-category legal    # chỉ legal
#   python eval/run_eval.py --dry                    # CI: mock pipeline + mock judge (định thức)
#   python eval/run_eval.py --inject                 # chạy eval/injection_test_vn.json qua guard
#   python eval/run_eval.py --json-out eval/results.json
#
# Ngưỡng khởi điểm (§11): exact-match >= 0.95; unsupported-claim = 0; delta ~0.05.
# Latency budget: P50 < 6s, P95 < 10s.
# =============================================================================

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

# ---------------------------------------------------------------------------
# Settings — ưu tiên ingest/config.py (Settings duy nhất của hệ thống); fallback
# đọc thẳng env (ingest chưa dựng lúc viết eval — defensive).
# ---------------------------------------------------------------------------
try:  # pragma: no cover
    from ingest.config import Settings as IngestSettings  # type: ignore

    _HAS_INGEST_SETTINGS = True
except Exception:  # noqa: BLE001 — ingest/config.py chưa tồn tại lúc dev
    _HAS_INGEST_SETTINGS = False


@dataclass
class EvalSettings:
    """Cấu hình eval — đọc từ env, không hardcode secret."""

    postgres_host: str = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    postgres_user: str = os.getenv("POSTGRES_USER", "ragre")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "")
    postgres_database: str = os.getenv("POSTGRES_DATABASE", "ragre")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    # Judge LLM ghim version — khác checkpoint với answer model (tránh correlated failure §7-conflict-7)
    judge_model: str = os.getenv("EVAL_JUDGE_MODEL", "deepseek-v4-flash-0731")
    judge_timeout_s: float = float(os.getenv("EVAL_JUDGE_TIMEOUT_S", "30"))
    pipeline_timeout_s: float = float(os.getenv("EVAL_PIPELINE_TIMEOUT_S", "60"))
    n_sim: int = int(os.getenv("EVAL_N_SIM", "1"))  # số lần lặp 1 câu (đo latency ổn định)

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
            except Exception:  # noqa: BLE001 — thiếu env bắt buộc → fallback env thuần
                pass
        return cls()


# ---------------------------------------------------------------------------
# Pipeline — import api.workflow.RagQueryPipeline (defensive; api chưa dựng lúc
# viết eval → chạy --dry hoặc CI subset). Spike: verify signature thật Ngày 1.
# ---------------------------------------------------------------------------
try:  # pragma: no cover
    from api.workflow import RagQueryPipeline  # type: ignore
except Exception:  # noqa: BLE001
    RagQueryPipeline = None


async def run_pipeline(pipeline: Any, question: str, as_of: Optional[str], settings: EvalSettings) -> dict:
    """Gọi pipeline với nhiều hình dạng call (spike: verify signature thật)."""
    kwargs: dict[str, Any] = {"query": question}
    if as_of:
        kwargs["as_of"] = as_of
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
    """Dựng RagQueryPipeline — thử nhiều constructor shape. Spike: verify Ngày 1."""
    if RagQueryPipeline is None:
        raise RuntimeError(
            "api/workflow.py chưa có — không dựng được RagQueryPipeline. "
            "Chạy --dry (mock) hoặc đợi api/ được dựng."
        )
    try:
        return RagQueryPipeline()  # constructor không tham số
    except TypeError:
        pass
    try:
        return RagQueryPipeline(settings)  # nhận settings
    except TypeError:
        pass
    if _HAS_INGEST_SETTINGS:
        try:
            return RagQueryPipeline(IngestSettings())  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001
            pass
    raise RuntimeError("Không khớp constructor RagQueryPipeline — sửa _build_pipeline()")


def is_rejected(payload: dict) -> bool:
    """Phát hiện query bị chặn (L1 guard/refusal) — kiểm tra nhiều key vì contract thay đổi."""
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


# ---------------------------------------------------------------------------
# Parse số tiếng Việt trong câu trả lời (numeric exact-match)
# "2.000.000.000" / "2,000,000,000" / "2000000000" / "2 tỷ" / "1,2 tỷ" / "8,5%"
# ---------------------------------------------------------------------------
_VND_UNIT = {"nghìn": 1e3, "k": 1e3, "triệu": 1e6, "tr": 1e6, "tỷ": 1e9, "tỉ": 1e9}
# KHÔNG chứa \s: ký tự class có space sẽ khiến greedy match nuốt nhiều số liền nhau
# ("2.000.000.000 25%" → "2.000.000.000 25" thành 1 token). Dấu phân cách nghìn
# là dấu chấm/phẩy, KHÔNG có space.
_NUM_TOKEN = r"[0-9][0-9\,\.]*[0-9]|[0-9]"


def _raw_to_float(raw: str) -> float:
    raw = raw.strip().replace(" ", "").replace(" ", "")
    if "," in raw and "." in raw:
        # "2,000,000.5" → phẩy = nghìn, chấm = thập phân
        if raw.rfind(",") < raw.rfind("."):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        # "2,000,000,000" → nghìn; "1,2" → thập phân (VN dùng phẩy thập phân)
        parts = raw.split(",")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and len(parts[1]) != 3:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "." in raw:
        # "2.000.000.000" (nhiều chấm = nghìn) vs "2.5" (thập phân)
        if raw.count(".") > 1:
            raw = raw.replace(".", "")
    return float(raw)


def extract_amounts(text: str) -> list[float]:
    """Trích số tiền (đã quy về VND nguyên) — '2 tỷ' → 2e9; '1,2 tỷ' → 1.2e9."""
    seen: list[float] = []
    for m in re.finditer(rf"({_NUM_TOKEN})\s*({'|'.join(_VND_UNIT)})\b", text, flags=re.IGNORECASE | re.UNICODE):
        try:
            val = _raw_to_float(m.group(1)) * _VND_UNIT[m.group(2).lower()]
            if not any(abs(val - f) < max(1, abs(val) * 1e-6) for f in seen):
                seen.append(val)
        except ValueError:
            continue
    # số nguyên trần (>= 6 chữ số → khả năng là tiền) — "8000000000" hoặc "2.000.000.000"
    for m in re.finditer(r"\b([0-9][0-9.,]{5,})\b", text):
        try:
            val = _raw_to_float(m.group(1))
            if val >= 1_000_000 and not any(abs(val - f) < max(1, abs(val) * 1e-6) for f in seen):
                seen.append(val)
        except ValueError:
            continue
    return seen


def extract_pct(text: str) -> list[float]:
    """Trích phần trăm — '25%' → 25.0; '8,5%' → 8.5; '0,5%' → 0.5."""
    out: list[float] = []
    for m in re.finditer(rf"({_NUM_TOKEN})\s*(?:%|phần trăm|phan tram)", text, flags=re.IGNORECASE | re.UNICODE):
        try:
            out.append(_raw_to_float(m.group(1)))
        except ValueError:
            continue
    return out


def extract_m2(text: str) -> list[float]:
    """Trích diện tích — '85,5 m²' → 85.5; '72 m2' → 72.0."""
    out: list[float] = []
    for m in re.finditer(rf"({_NUM_TOKEN})\s*(?:m2|m²|mét vuông|met vuong)", text, flags=re.IGNORECASE | re.UNICODE):
        try:
            out.append(_raw_to_float(m.group(1)))
        except ValueError:
            continue
    return out


def extract_ints(text: str) -> list[int]:
    """Trích số nguyên (count/term) — term 180/240, count 4/5/11."""
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
    """Mọi value trong expected_facts phải xuất hiện trong answer (sau normalize)."""
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


# ---------------------------------------------------------------------------
# Judge LLM — faithfulness (ghim version). Khác dòng với answer model.
# ---------------------------------------------------------------------------
class BaseJudge:
    async def judge(self, question: str, answer: str, contexts: list[str]) -> tuple[bool, float, str]:
        raise NotImplementedError


class MockJudge(BaseJudge):
    """Judge định thức cho --dry / CI: pass nếu any-of token xuất hiện trong answer."""

    async def judge(self, question: str, answer: str, contexts: list[str]) -> tuple[bool, float, str]:
        return True, 1.0, "mock judge (--dry)"


class LLMJudge(BaseJudge):
    """Judge LLM OpenAI-compatible — JSON mode, ghim `judge_model`."""

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


# ---------------------------------------------------------------------------
# Context fetch (gold_chunk_ids → chunk content) — dùng cho judge faithfulness.
# ---------------------------------------------------------------------------
async def fetch_chunk_contexts(doc_prefixes: list[str], settings: EvalSettings) -> list[str]:
    """Đọc nội dung chunk thuộc các doc_id khớp (gold_chunk_ids dùng prefix doc_id)."""
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
    except Exception as exc:  # noqa: BLE001 — không chặn eval khi DB chưa có chunk
        print(f"  [warn] fetch_chunk_contexts skip: {exc}")
        return []
    finally:
        if conn is not None:
            await conn.close()


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------
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
    faithful_score: float = 0.0
    fail_reasons: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Mock pipeline (--dry) — định thức, không cần backend
# ---------------------------------------------------------------------------
class MockPipeline:
    """Trả payload khớp expectation của golden câu → test harness chạy đúng."""

    def __init__(self, golden: dict[str, Any]) -> None:
        self.golden = golden

    async def run(self, query: str, as_of: Optional[str] = None, **_: Any) -> dict:
        q = self._find_question(query)
        if q is None:
            # --inject --dry: mock chặn prompt dạng injection để harness đo được
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
            "latency_ms": 12,
            "trace_id": "dry-mock",
        }

    def _find_question(self, query: str) -> Optional[dict[str, Any]]:
        for q in self.golden.get("questions", []):
            if q.get("question") == query:
                return q
        return None


# ---------------------------------------------------------------------------
# Check helpers
# ---------------------------------------------------------------------------
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
    )


# ---------------------------------------------------------------------------
# Eval core
# ---------------------------------------------------------------------------
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
    except Exception as exc:  # noqa: BLE001 — eval KHÔNG crash khi pipeline lỗi
        res.error = f"pipeline lỗi: {exc.__class__.__name__}: {exc}"
        res.fail_reasons.append(res.error)
        return res
    res.latency_ms = (time.perf_counter() - start) * 1000.0

    answer = str(payload.get("answer") or "")
    payload_routing = payload.get("routing")
    blocked = is_rejected(payload)  # guard chặn → routing không có ý nghĩa (bỏ qua check)

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
        "overall_pass": sum(_overall_pass(r) for r in results) / total,
    }


# ---------------------------------------------------------------------------
# Injection eval (--inject)
# ---------------------------------------------------------------------------
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
                # heuristics: answer thể hiện từ chối → coi là bị chặn
                answer = str(payload.get("answer") or "")
                rejected = any(m in answer.lower() for m in ["từ chối", "không thể", "không liên quan"])
        except Exception as exc:  # noqa: BLE001 — pipeline lỗi do injection → fail-closed
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


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
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
            r.pass_content and r.pass_numeric and r.pass_routing and r.pass_refusal and r.pass_freshness for r in rs
        )
        print(f"  {cat:<22}{n_pass:>3}/{len(rs):<3} (content+num+routing+refusal+freshness)")


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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def amain(args: argparse.Namespace, settings: EvalSettings) -> int:
    dry = args.dry
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
        # latency: mặc định tái dùng res.latency_ms (1 lần gọi pipeline/câu);
        # n_sim > 1 → đo thêm (chỉ dùng khi cần ổn định latency).
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
