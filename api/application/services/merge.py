"""Merge, hydrate, and build context blocks (plan §4.6).

hydrate_chunks attaches doc metadata (title/section/dates/kind) from the registry
so generation cites correct sources. RAG_CONTEXT and FACT_EVIDENCE blocks are
JSON-encoded with an internal-key strip; sources[] dedups by doc_id for the UI.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ingest.placeholder import FACT_TOKEN_START, extract_placeholders, resolve_placeholders
from ingest.subject_key import normalize_subject_key

from .fact_display import enrich_evidence_entry
from .sql_leg import get_ro_pool

logger = logging.getLogger("api.merge")

DELIMITER = "=" * 60

_RAG_AREA_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?P<label>nha_tre|shcd_tang2|shcd_tang3|be_boi|gym|yoga)"
    r"(?![A-Za-z0-9_])\s*(?:[:=]\s*)?(?:diện\s+tích\s*)?(?P<area>\d+(?:[.,]\d+)?)\s*m(?:2|²)(?!\w)",
    
    re.IGNORECASE,
)


def _rag_area_evidence(chunk: dict, record: dict, project_key: str | None, as_of: date | None) -> list[tuple[Decimal, str, str, str, str, str]]:
    """Return registry-backed amenity areas only for an exact selected chunk match."""
    registry_content = record.get("registry_content")
    if not _eligible_chunk(chunk, record, project_key, as_of):
        return []
    try:
        if record.get("section", "").strip().casefold() != "tiện ích":
            return []
    except (AttributeError, TypeError):
        return []
    evidence = []
    for match in _RAG_AREA_PATTERN.finditer(registry_content):
        try:
            area = Decimal(match.group("area").replace(",", "."))
        except InvalidOperation:
            continue
        if area > 0:
            evidence.append(
                (area, match.group("label").lower(), record["doc_id"], record["title"], chunk["id"], project_key)
            )
    return evidence


@dataclass
class Merged:
    rag_blocks: str
    evidence_blocks: str
    sources: list[dict]
    facts: list[dict]
    meta: dict = field(default_factory=dict)  # rewritten/query/as_of/degraded/model/prompt_hash/...


def _iso(v: Any) -> str | None:
    if isinstance(v, date):
        return v.isoformat()
    return v if v is not None else None


def _strip_internal(chunk: dict) -> dict:
    """Drop internal keys (leading '_') before JSON-encoding to the LLM/UI."""
    return {k: v for k, v in chunk.items() if not k.startswith("_")}


def _format_fact_value(row: dict) -> str | None:
    """Render a facts row as LLM-readable text; range facts keep both bounds."""
    unit = row.get("unit") or ""
    if row.get("quality") == "range":
        lo, hi = row.get("range_min"), row.get("range_max")
        if lo is None and hi is None:
            return None
        return f"{lo}–{hi} {unit}".strip()
    value = row.get("value_num")
    if value is None:
        value = row.get("value_text")
    if value is None:
        return None
    return f"{value} {unit}".strip()


async def _resolve_fact_value(
    conn: Any,
    fact_key: str,
    subject_key: str,
    policy_key: str | None,
    as_of: date | None,
    project_key: str | None = None,
) -> str | None:
    """Formatted value of the fact in effect at as_of; None when absent/expired.

    ``project_key`` (story 10.4): subject_key alone is not globally unique
    (identical subject codes can exist in two projects), so the fact lookup is
    scoped to the request's project. None keeps the legacy unscoped read for
    project-less callers.
    """
    row = await conn.fetchrow(
        """
        SELECT f.value_num, f.value_text, f.unit, f.quality, f.range_min, f.range_max
        FROM facts f
        JOIN fact_subjects fs ON fs.id = f.subject_id
        JOIN documents d ON d.doc_id = f.source_doc_id
        WHERE fs.subject_key = $1 AND f.fact_key = $2
          AND ($3::text IS NULL OR f.policy_key = $3)
          AND ($4::text IS NULL OR fs.project_key = $4)
          AND d.status = 'published'
          AND f.effective_from <= $5
          AND (f.effective_to IS NULL OR f.effective_to > $5)
        ORDER BY f.effective_from DESC
        LIMIT 1
        """,
        normalize_subject_key(subject_key),
        fact_key,
        policy_key,
        project_key,
        as_of or date.today(),
    )
    if row is None:
        return None
    return _format_fact_value(row)


async def _resolve_chunk_placeholders(
    chunks: list[dict], conn: Any, as_of: date | None, project_key: str | None = None
) -> None:
    """Hydrate ⟦FACT tokens in each chunk's content from the facts registry (in place)."""
    refs = {r for c in chunks for r in extract_placeholders(c.get("content") or "")}
    if not refs:
        return
    cache: dict[tuple[str, str, str | None], str | None] = {}
    for ref in refs:
        cache[(ref.fact_key, ref.subject_key, ref.policy_key)] = await _resolve_fact_value(
            conn, ref.fact_key, ref.subject_key, ref.policy_key, as_of, project_key
        )
    for c in chunks:
        content = c.get("content") or ""
        if FACT_TOKEN_START in content:
            c["content"] = resolve_placeholders(content, lambda fk, sk, pk: cache.get((fk, sk, pk)))


def _eligible_chunk(chunk: dict, record: dict, project_key: str | None, as_of: date | None) -> bool:
    content, registry_content = chunk.get("content"), record.get("registry_content")
    start, end = record.get("effective_from"), record.get("effective_to")
    try:
        effective_on = as_of or date.today()
        if isinstance(start, str):
            start = date.fromisoformat(start)
        if isinstance(end, str):
            end = date.fromisoformat(end)
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()
        return bool(
            isinstance(chunk.get("id"), str) and chunk["id"].strip()
            and isinstance(content, str) and content.strip()
            and isinstance(registry_content, str) and registry_content.strip()
            and content == registry_content
            and isinstance(project_key, str) and project_key.strip()
            and record.get("project_key") == project_key
            and record.get("status") == "published"
            and isinstance(start, date) and start <= effective_on
            and (end is None or (isinstance(end, date) and end > effective_on))
            and isinstance(record.get("doc_id"), str) and record["doc_id"].strip()
            and isinstance(record.get("title"), str) and record["title"].strip()
        )
    except (AttributeError, TypeError, ValueError):
        return False


async def hydrate_chunks(
    chunks: list[dict], as_of: date | None = None, project_key: str | None = None
) -> list[dict]:
    """Return only exact-content chunks authorized by the document registry."""
    if not chunks:
        return []
    ids = [c.get("id") for c in chunks if c.get("id")]
    if not ids:
        return []
    try:
        pool = await get_ro_pool()
        sql = """SELECT c.chunk_id, c.content AS registry_content, c.section,
                        d.doc_id, d.title, d.kind, d.project_key, d.status,
                        d.effective_from, d.effective_to
                 FROM document_chunks c JOIN documents d ON d.doc_id = c.doc_id
                 WHERE c.chunk_id = ANY($1)"""
        async with pool.acquire() as conn:
            recs = await conn.fetch(sql, ids)
            lookup = {r["chunk_id"]: r for r in recs}
            eligible = [c for c in chunks if _eligible_chunk(c, lookup.get(c.get("id"), {}), project_key, as_of)]
            out = [
                {
                    **_strip_internal(c),
                    "doc_id": lookup[c["id"]].get("doc_id"),
                    "title": lookup[c["id"]].get("title"),
                    "section": lookup[c["id"]].get("section"),
                    "project_key": lookup[c["id"]].get("project_key"),
                    "status": lookup[c["id"]].get("status"),
                    "effective_from": _iso(lookup[c["id"]].get("effective_from")),
                    "effective_to": _iso(lookup[c["id"]].get("effective_to")),
                    "_rag_area_evidence": _rag_area_evidence(c, lookup[c["id"]], project_key, as_of),
                }
                for c in eligible
            ]
            await _resolve_chunk_placeholders(out, conn, as_of, project_key)
            out = [chunk for chunk in out if isinstance(chunk.get("content"), str) and chunk["content"].strip()]
        return out
    except Exception as exc:  # noqa: BLE001 — registry failure fails closed
        logger.warning("merge.hydrate fail: %s", exc)
        return []


def build_rag_context(chunks: list[dict]) -> str:
    """RAG_CONTEXT block — JSON-encode the hydrated chunks (L2: delimiter + JSON)."""
    payload = [
        {
            "id": c.get("id"),
            "score": c.get("score"),
            "content": c.get("content"),
            "doc_id": c.get("doc_id"),
            "title": c.get("title"),
            "section": c.get("section"),
            "project_key": c.get("project_key"),
            "status": c.get("status"),
            "effective_from": c.get("effective_from"),
            "effective_to": c.get("effective_to"),
        }
        for c in chunks
    ]
    return f"{DELIMITER}\nRAG_CONTEXT (chunks từ LightRAG, đã lọc hiệu lực + rerank):\n{json.dumps(payload, ensure_ascii=False)}\n{DELIMITER}"  # noqa: E501


def build_evidence_context(evidence: list[dict]) -> str:
    """FACT_EVIDENCE block — the sole source for business/financial numbers.

    Amenity-area claims have a separate, narrow RAG registry-evidence exception.
    Entries gain additive ``subject_display`` / ``policy_display`` legends (D4) so
    the model names units and payment methods in prose instead of echoing machine
    keys. Backfilling here covers every producer (SQL leg, affordability, NL2SQL);
    machine keys are preserved untouched because guard_output grounds numbers on
    ``fields`` and citations on ``fe_id``.
    """
    enriched = [enrich_evidence_entry(e) for e in evidence or []]
    return f"{DELIMITER}\nFACT_EVIDENCE (số liệu từ hệ thống dữ liệu — nguồn duy nhất cho số liệu kinh doanh/tài chính, LLM không tự tính; khi gọi tên căn hay phương án thanh toán CHỈ dùng subject_display/policy_display, KHÔNG viết subject/policy_key nội bộ):\n{json.dumps(enriched, ensure_ascii=False)}\n{DELIMITER}"  # noqa: E501


def build_sources(chunks: list[dict]) -> list[dict]:
    """sources[] for the UI — dedup by doc_id, keeping first-seen order."""
    seen: set[str] = set()
    sources: list[dict] = []
    for c in chunks:
        doc_id = c.get("doc_id")
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        sources.append(
            {
                "doc_id": doc_id,
                "title": c.get("title") or doc_id,
                "section": c.get("section"),
                "effective_from": c.get("effective_from"),
                "kind": c.get("kind"),
            }
        )
    return sources


def build_facts(evidence: list[dict]) -> list[dict]:
    """facts[] for the UI — only display fields from fe blocks."""
    return [
        {
            "fe_id": e.get("fe_id"),
            "subject": e.get("subject"),
            "policy_key": e.get("policy_key"),
            "fields": e.get("fields", {}),
            "note": e.get("note"),
        }
        for e in evidence
    ]


async def merge_context(
    query: str,
    rag_chunks: list[dict],
    evidence: list[dict],
    as_of: date | None,
    project_key: str | None = None,
) -> Merged:
    """Combine both legs into context blocks plus sources/facts for the UI.

    ``project_key`` (story 10.4): scopes the FACT-placeholder hydration so a
    chunk can never pull a fact value from another project's subject.
    """
    hydrated = await hydrate_chunks(rag_chunks, as_of, project_key) if rag_chunks and project_key else []
    rag_blocks = build_rag_context(hydrated)
    evidence_blocks = build_evidence_context(evidence or [])
    rag_rows = json.loads(rag_blocks.split("\n", 3)[2])
    fact_rows = json.loads(evidence_blocks.split("\n", 3)[2])
    sources = build_sources(hydrated) if rag_rows else []
    facts = build_facts(evidence or [])
    selected_chunk_ids = tuple(row["id"] for row in rag_rows if isinstance(row.get("id"), str) and row["id"].strip() and isinstance(row.get("content"), str) and row["content"].strip())
    selected_project_keys = tuple(dict.fromkeys(row["project_key"] for row in rag_rows if row.get("id") in selected_chunk_ids and isinstance(row.get("project_key"), str) and row["project_key"].strip()))
    selected_fact_ids = tuple(row["fact_id"] for row in fact_rows if row.get("fact_id") is not None and row.get("fact_id") != "")
    selected_fe_ids = tuple(row["fe_id"] for row in fact_rows if isinstance(row.get("fe_id"), str) and row["fe_id"].strip())
    rag_area_evidence = [item for chunk in hydrated for item in chunk.get("_rag_area_evidence", [])]
    return Merged(
        rag_blocks=rag_blocks,
        evidence_blocks=evidence_blocks,
        sources=sources,
        facts=facts,
        meta={
            "_rag_area_evidence": rag_area_evidence,
            "selected_chunk_ids": selected_chunk_ids,
            "selected_project_keys": selected_project_keys,
            "selected_fact_ids": selected_fact_ids,
            "selected_fe_ids": selected_fe_ids,
        },
    )
