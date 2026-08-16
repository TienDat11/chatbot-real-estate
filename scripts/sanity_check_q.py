#!/usr/bin/env python3
"""Story 2.4 retrieval sanity check — drive POST /query (SSE) on real infra.

Sends 10 spot-check questions through the real FastAPI + PG + LLM pipeline,
collects sources/facts/token/done events, runs hydrate checks against the
registry (document_chunks), and logs a verdict report to
eval/reports/ingest-sanity-2026-08.md.

Ground truth expectations come from data/_processed/feed_back/feedback_data.txt
([GT-Bx.y] rows); numbers are never invented here.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import asyncpg
import httpx

API_BASE = "http://127.0.0.1:8000"
# Repo root = this script's directory; paths stay correct regardless of CWD (code review f9).
REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / "eval" / "reports" / "ingest-sanity-2026-08.md"
SSE_TIMEOUT_S = 200  # server global workflow cap is 180s — keep client margin
CONNECT_TIMEOUT_S = 10


@dataclass
class Question:
    id: str
    question: str
    as_of: str | None = None
    contains: list[str] = field(default_factory=list)
    # At least ONE of these must appear (e.g. either date representation).
    contains_any: list[str] = field(default_factory=list)
    not_contains: list[str] = field(default_factory=list)
    # Tokens that must NOT be asserted as fact; echoing the question inside a
    # refusal is acceptable (e.g. "469 hay 428" echoed while declining).
    not_assert: list[str] = field(default_factory=list)
    # not_assert counter: token -> tokens that defuse the assertion (official
    # number stated alongside the historical mention, e.g. 428 + 469).
    not_assert_counters: dict[str, list[str]] = field(default_factory=dict)
    # When True, flag actual law-article citations like "Điều 17" (not words
    # like "điều chỉnh").
    not_law_citation: bool = False
    expect_refusal: bool = False
    min_sources: int = 0
    # Grounding: tokens that must appear in at least one cited chunk content.
    ground_span: list[str] = field(default_factory=list)
    note: str = ""


QUESTIONS: list[Question] = [
    Question(
        id="q1-legal-qd254",
        question=(
            "Quyết định 254 của UBND tỉnh về chủ trương đầu tư lần đầu có ngày ban hành khi nào?"
        ),
        # Either date representation is a correct answer (code review f2).
        contains_any=["31/01/2024", "31 tháng 01 năm 2024"],
        not_assert=["14/01/2019"],
        min_sources=1,
        ground_span=["31/01/2024", "254"],
        note="GT-B1.7 ngày cấp lần đầu = 31/01/2024, phải kèm citation legal.",
    ),
    Question(
        id="q2-price-2pn-noi-khu",
        question="Căn hộ 2 phòng ngủ view nội khu tại The Camellia giá bao nhiêu?",
        contains=["tỷ"],
        min_sources=0,
        ground_span=["2PN", "nội khu", "tỷ"],
        note="Giá trả theo range/band (không bịa số lẻ) — nguồn price facts.",
    ),
    Question(
        id="q3-utility",
        question="The Camellia có những tiện ích gì?",
        min_sources=1,
        ground_span=["Camellia"],
        note="Tiện ích từ doc project — phải có citation.",
    ),
    Question(
        id="q4-deposit",
        question="Khi đặt cọc mua căn hộ The Camellia thì khách cọc bao nhiêu tiền?",
        contains=["100", "triệu"],
        not_contains=["10 triệu"],
        ground_span=["100", "cọc"],
        note="GT-B1.3 cọc 100 triệu mọi phương thức.",
    ),
    Question(
        id="q5-capacity-469",
        question="Dự án The Camellia có công suất bao nhiêu căn hộ, 469 hay 428?",
        contains=["469"],
        not_contains=["Luật"],
        not_assert=["428"],
        not_assert_counters={"428": ["469", "479"]},
        not_law_citation=True,
        min_sources=1,
        ground_span=["469"],
        note="GT-B5: trả 469+10 TMDV, KHÔNG nêu ai công bố/KHÔNG trích luật/KHÔNG trả 428.",
    ),
    Question(
        id="q6-out-of-scope",
        question="Bánh mì ngon nhất Đà Nẵng bán ở đâu?",
        expect_refusal=True,
        min_sources=0,
        note="Câu ngoài ngành — hệ thống phải từ chối đúng.",
    ),
    Question(
        id="q7-as-of",
        question="Khách cọc bao nhiêu khi mua căn hộ The Camellia?",
        as_of="2026-08-01",
        not_contains=["100 triệu"],
        min_sources=0,
        note=(
            "as_of trước effective_from của price-camellia-2026q3 (2026-08-13) — "
            "không được trả số liệu chưa hiệu lực."
        ),
    ),
    Question(
        id="q8-vay-bu-dap",
        question="Khách có được vay bù đắp khi mua căn hộ tại dự án này không?",
        contains=["khi"],
        not_contains=["khu mua"],
        ground_span=["vay bù đắp"],
        note="GT-B6.3 lỗi đánh máy: đúng là 'khi mua', không phải 'khu mua'.",
    ),
    Question(
        id="q9-50-ngay",
        question="Có phải nhà nước cấp sổ đỏ trong 50 ngày không?",
        not_contains=["trong 50 ngày", "cấp sổ trong 50"],
        min_sources=0,
        ground_span=["50 ngày"],
        note="GT-B6.2: 50 ngày là hạn chủ đầu tư GỬI hồ sơ, không phải thời hạn cấp sổ.",
    ),
    Question(
        id="q10-mbv",
        question="Những ngân hàng nào tham gia gói ưu đãi lãi suất cho dự án?",
        contains=["MBV"],
        min_sources=1,
        ground_span=["MBV"],

        note="GT-B6.4 MBV trạng thái 'dự kiến'.",
    ),
]


def load_env(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict (supports URLs with ':' — no shell sourcing)."""
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def parse_sse_frames(chunk: bytes) -> list[tuple[str, str]]:
    """Split raw SSE bytes into (event, raw_data) frames at blank-line boundaries."""
    text = chunk.decode("utf-8", errors="replace")
    frames: list[tuple[str, str]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = ""
        data_parts: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_parts.append(line[5:].strip())
        if data_parts:
            frames.append((event, "\n".join(data_parts)))
    return frames


async def stream_query(client: httpx.AsyncClient, q: Question) -> dict:
    """POST /query with SSE Accept; returns collected answer/sources/facts/meta."""
    payload: dict[str, object] = {"query": q.question}
    if q.as_of:
        payload["as_of"] = q.as_of
    collected: dict[str, object] = {"tokens": [], "sources": [], "facts": [], "done": None}
    buffer = ""
    async with client.stream(
        "POST",
        f"{API_BASE}/query",
        json=payload,
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(SSE_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
    ) as resp:
        if resp.status_code != 200:
            return {**collected, "http_error": resp.status_code}
        async for raw in resp.aiter_bytes():
            # Frames may split across network chunks — accumulate then cut at \n\n.
            buffer += raw.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                for event, data in parse_sse_frames(f"{block}\n\n".encode()):
                    await _absorb(collected, event, data)
    # Flush a trailing frame with no closing blank line.
    if buffer.strip():
        for event, data in parse_sse_frames(buffer.encode()):
            await _absorb(collected, event, data)
    collected["answer"] = "".join(str(t) for t in collected["tokens"])
    return collected


async def _absorb(collected: dict, event: str, data: str) -> None:
    """Route one parsed SSE frame into the collected result."""
    if not data:
        return
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return
    if event == "token" and isinstance(obj.get("text"), str):
        collected["tokens"].append(obj["text"])
    elif event == "sources" and isinstance(obj.get("sources"), list):
        collected["sources"] = obj["sources"]
    elif event == "facts" and isinstance(obj.get("facts"), list):
        collected["facts"] = obj["facts"]
    elif event == "done":
        collected["done"] = obj
    elif event == "error":
        collected["error"] = obj.get("message", "error event")


async def fetch_registry(conn) -> dict[str, list[dict]]:
    """doc_id -> list of chunk records (content + chunk_id + section)."""
    rows = await conn.fetch(
        """
        SELECT c.doc_id, c.chunk_id, c.content, c.section
        FROM document_chunks c
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE d.status = 'published'
        ORDER BY c.doc_id, c.chunk_id
        """
    )
    by_doc: dict[str, list[dict]] = {}
    for row in rows:
        by_doc.setdefault(row["doc_id"], []).append(
            {"chunk_id": row["chunk_id"], "content": row["content"], "section": row["section"]}
        )
    return by_doc


async def hydrate_check(
    sources: list[dict], registry: dict[str, list[dict]], spans: list[str]
) -> tuple[bool, list[str]]:
    """Verify source doc_ids exist in registry and each span appears in a cited chunk."""
    if not sources:
        return False, ["no sources to ground"]
    failures: list[str] = []
    doc_ids = [s.get("doc_id") for s in sources if s.get("doc_id")]
    missing = [d for d in doc_ids if d not in registry]
    if missing:
        failures.append(f"doc not in registry: {missing}")
    if not spans:
        return not failures, failures or ["ok"]
    corpus = " ".join(
        c["content"] for d in doc_ids if d in registry for c in registry[d]
    )
    for span in spans:
        if span not in corpus:
            failures.append(f"span not found in cited docs: '{span}'")
    return not failures, failures or ["ok"]


async def run() -> int:
    env = load_env(REPO_ROOT / ".env")
    # Keyword args (no DSN string) — credentials with URL specials (@ : / %) stay
    # safe and never end up misparsed into host/user (security review F3).
    try:
        pool = await asyncpg.create_pool(
            host=env.get("POSTGRES_HOST", "localhost"),
            port=int(env.get("POSTGRES_PORT", "5432")),
            user=env.get("POSTGRES_USER"),
            password=env.get("POSTGRES_PASSWORD"),
            database=env.get("POSTGRES_DATABASE"),
            command_timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] cannot open PG pool: {exc}", file=sys.stderr)
        return 2

    try:
        async with pool.acquire() as conn:
            registry = await fetch_registry(conn)
    finally:
        await pool.close()

    results: list[dict] = []
    http = httpx.AsyncClient()
    try:
        for q in QUESTIONS:
            print(f"\n=== {q.id} === {q.question}")
            try:
                col = await stream_query(http, q)
            except Exception as exc:  # noqa: BLE001 — one bad query must not kill the run
                col = {"error": f"client exception: {exc}"}
            answer = str(col.get("answer", ""))
            sources = list(col.get("sources", []))
            facts = list(col.get("facts", []))
            print(f"  answer: {answer[:300]}")
            print(f"  sources: {[s.get('doc_id') for s in sources]}")
            print(f"  facts: {[f.get('fe_id', '') for f in facts]}")

            verdict: list[tuple[bool, str]] = []
            if col.get("http_error") == 400 and q.expect_refusal:
                verdict.append((True, "HTTP 400 guard refusal (correct)"))
            elif col.get("http_error"):
                verdict.append((False, f"HTTP {col['http_error']}"))
            elif col.get("error"):
                verdict.append((False, f"error event: {col['error']}"))
            else:
                if not answer.strip():
                    verdict.append((False, "empty answer"))
                if q.expect_refusal:
                    refused_keywords = (
                        "ngoài phạm vi", "không thể trả lời", "không thuộc",
                        "chỉ hỗ trợ", "không thể hỗ trợ", "không có dữ liệu",
                        "không thể xác nhận", "không nằm trong phạm vi", "ngoài lĩnh vực",
                    )
                    ok = "bánh mì" not in answer.lower() or any(
                        k in answer.lower() for k in refused_keywords
                    )
                    verdict.append((ok, "refusal" if ok else f"did not refuse: {answer[:120]}"))
                for token in q.contains:
                    ok = token.lower() in answer.lower()
                    verdict.append((ok, f"contains '{token}'" if ok else f"missing '{token}'"))
                if q.contains_any:
                    low = answer.lower()
                    hit = [t for t in q.contains_any if t.lower() in low]
                    ok = bool(hit)
                    verdict.append((ok, f"contains_any {hit}" if ok else f"missing all {q.contains_any}"))
                for token in q.not_contains:
                    ok = token.lower() not in answer.lower()
                    label = f"excludes '{token}'" if ok else f"found forbidden '{token}'"
                    verdict.append((ok, label))
                if q.not_law_citation and re.search(r"[Đđ]iều\s+\d+", answer):
                    verdict.append((False, "found law article citation 'Điều N'"))
                # not_assert: token must not be ASSERTED as fact. Echoing the
                # question or mentioning it inside a refusal/hedge is acceptable.
                hedge = ("chưa có thông tin", "không có dữ liệu", "không thể xác nhận",
                         "không thể xác thực", "ngoài phạm vi", "không thuộc", "chưa xác")
                for token in q.not_assert:
                    low = answer.lower()
                    tok = token.lower()
                    if tok not in low:
                        ok = True
                    elif any(h in low for h in hedge):
                        ok = True  # hedged/refusal frame — acceptable echo
                    else:
                        # Asserted without hedge: acceptable only when an official
                        # counter-number is stated alongside (defuses the mention).
                        counters = q.not_assert_counters.get(token, [])
                        ok = any(c.lower() in low for c in counters)
                    label = f"does not assert '{token}'" if ok else f"asserted forbidden '{token}'"
                    verdict.append((ok, label))
                if len(sources) < q.min_sources:
                    verdict.append((False, f"need >= {q.min_sources} sources, got {len(sources)}"))
                # Grounding is only required when a RAG answer is expected;
                # refusals and as-of-emptied results legitimately return none.
                if q.expect_refusal or q.as_of:
                    verdict.append((True, "grounding skipped (refusal/as_of)"))
                else:
                    ok_ground, reasons = await hydrate_check(sources, registry, q.ground_span)
                    verdict.append((ok_ground, "; ".join(reasons)))


            passed = all(ok for ok, _ in verdict)
            results.append(
                {
                    "id": q.id,
                    "question": q.question,
                    "as_of": q.as_of,
                    "answer": answer,
                    "sources": sources,
                    "facts": facts,
                    "checks": [{"ok": ok, "label": label} for ok, label in verdict],
                    "pass": passed,
                }
            )
            print(f"  => {'PASS' if passed else 'FAIL'}: {[lb for ok, lb in verdict if not ok]}")
    finally:
        await http.aclose()

    write_report(
        results,
        chunks=sum(len(chunks) for chunks in registry.values()),
        docs=len(registry),
    )
    passed_count = sum(1 for r in results if r["pass"])
    print(f"\nRESULT: {passed_count}/10 passed")
    return 0 if passed_count == 10 else 1


def write_report(results: list[dict], chunks: int, docs: int) -> None:
    """Write the log report with answers, citations, and verdicts."""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    lines = [
        "# Ingest Sanity Check — 2026-08",
        "",
        f"> Story 2.4 · Date {today} · Real infra: FastAPI :8000 + PG + LightRAG 1.5.6"
        f" + aibox rerank (hybrid mode). {chunks} chunks / {docs} docs verified.",
        "",
        "## Verdict",
        "",
    ]
    for r in results:
        lines.append(f"- **{r['id']}** — {'✅ PASS' if r['pass'] else '❌ FAIL'} · {r['question']}")
    lines += ["", "## Detail", ""]
    for r in results:
        checks = " · ".join(("✅" if c["ok"] else "❌") + c["label"] for c in r["checks"])
        lines += [
            f"### {r['id']} — {'PASS' if r['pass'] else 'FAIL'}",
            f"**Q:** {r['question']}" + (f" (as_of={r['as_of']})" if r["as_of"] else ""),
            f"**A:** {r['answer']}",
            f"**Sources:** {[s.get('doc_id') for s in r['sources']]} "
            f"({len(r['sources'])} cited)",
            f"**Facts:** {[f.get('fe_id') for f in r['facts']]}",
            f"**Checks:** {checks}",
            "",
        ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    # Fail fast when PG is unreachable (no mock path in this script).
    env = load_env(REPO_ROOT / ".env")
    try:
        with socket.create_connection(
            (env.get("POSTGRES_HOST", "localhost"), int(env.get("POSTGRES_PORT", "5432"))),
            timeout=3,
        ):
            pass
    except OSError as exc:
        print(f"[FATAL] PG unreachable: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())