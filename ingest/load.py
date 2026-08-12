"""Load — THE transaction: registry (1 tx) → LightRAG ainsert (SAU COMMIT).

Plan §3.2 step 5-6: documents → fact_subjects → document_chunks → facts → chunk_fact_refs
→ campaigns → COMMIT; ainsert SAU COMMIT (pool riêng), ids/file_paths 1:1, lightrag_doc_id.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import asyncpg

from ingest.config import settings
from ingest.fact_extract import ExtractedFact, extract_facts
from ingest.parser import ParsedDoc, parse_document
from ingest.placeholder import replace_fact_with_placeholder, sanitize_forged_tokens

logger = logging.getLogger(__name__)


class LoadError(RuntimeError):
    """Rollback đã xảy ra — context đính kèm để ghi ingest_log/review."""


@dataclass
class LoadResult:
    doc_id: str
    version: int
    chunk_count: int
    fact_count: int
    lightrag_doc_id: str | None


def _normalize_subject_key(key: str) -> str:
    """subject_key dedup: strip dấu chấm-gạch, lowercase trước UNIQUE (plan §3.2 step 4)."""
    return key.strip().lower().replace(".", "").replace("-", "")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _upsert_campaign(conn, campaign_key: str, project_key: str, effective_from, effective_to, source_doc_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO campaigns (campaign_key, project_key, effective_from, effective_to, source_doc_id, status)
        VALUES ($1, $2, $3, $4, $5, 'active')
        ON CONFLICT (campaign_key) DO UPDATE
          SET effective_from = EXCLUDED.effective_from,
              effective_to   = EXCLUDED.effective_to,
              source_doc_id  = EXCLUDED.source_doc_id,
              status         = 'active'
        """,
        campaign_key, project_key, effective_from, effective_to, source_doc_id,
    )


async def _upsert_subject(conn, fact: ExtractedFact) -> int:
    display = fact.subject_display or fact.subject_key
    project_key = None
    if fact.subject_type == "unit" and "/" in fact.subject_key:
        project_key = fact.subject_key.split("/")[0].split(":")[-1]
    row = await conn.fetchrow(
        """
        INSERT INTO fact_subjects (subject_key, subject_type, display_name, project_key, attrs)
        VALUES ($1, $2, $3, $4, '{}')
        ON CONFLICT (subject_key) DO UPDATE
          SET display_name = EXCLUDED.display_name,
              project_key  = COALESCE(EXCLUDED.project_key, fact_subjects.project_key)
        RETURNING id
        """,
        _normalize_subject_key(fact.subject_key), fact.subject_type, display, project_key,
    )
    return int(row["id"])


async def _insert_fact(
    conn,
    fact: ExtractedFact,
    subject_id: int,
    doc_id: str,
    chunk_id: str,
    effective_from,
    effective_to,
) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO facts (
          subject_id, fact_key, policy_key, campaign_key, value_num, value_text, unit,
          quality, range_min, range_max, volatile, effective_from, effective_to,
          source_doc_id, source_chunk_id, extract_conf
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
        RETURNING id
        """,
        subject_id,
        fact.fact_key,
        fact.policy_key,
        fact.campaign_key,
        fact.value_num,
        fact.value_text,
        fact.unit,
        fact.quality,
        fact.range_min,
        fact.range_max,
        True,  # volatile
        effective_from,
        effective_to,
        doc_id,
        chunk_id,
        fact.extract_conf,
    )
    return int(row["id"])


async def load_document(parsed: ParsedDoc, facts: list[ExtractedFact] | None = None) -> LoadResult:
    """Ghi registry trong 1 transaction, sau đó ainsert LightRAG.

    Args:
        parsed: ParsedDoc từ parser.
        facts: facts đã extract (None → chỉ ghi chunks, facts đi đường khác).

    Raises:
        LoadError: rollback + message (caller ghi review_queue/ingest_log).
    """
    chunks = parsed.sections
    conn = await asyncpg.connect(settings.pg_dsn)
    version = 1
    fact_rows: list[tuple[int, str]] = []  # (fact_id, chunk_id)
    eff_from = _effective_from(parsed)
    eff_to = _effective_to(parsed)
    try:
        async with conn.transaction():
            # 1) documents upsert (version+1 nếu đã tồn tại)
            doc_row = await conn.fetchrow(
                """
                INSERT INTO documents (
                  doc_id, kind, title, source_file, effective_from, effective_to,
                  status, content_hash, version, metadata
                ) VALUES ($1,$2,$3,$4,$5,$6,'published',$7,1,'{}')
                ON CONFLICT (doc_id) DO UPDATE
                  SET status='published',
                      version = documents.version + 1,
                      content_hash = EXCLUDED.content_hash,
                      updated_at = now()
                RETURNING version
                """,
                parsed.doc_id, parsed.kind, parsed.title, parsed.source_file,
                eff_from, eff_to, parsed.content_hash,
            )
            version = int(doc_row["version"])
            chunk_ids = [f"{parsed.doc_id}:{version}:{i}" for i in range(len(chunks))]

            # 2) true replace on re-ingest: drop the previous version's facts + chunks
            #    (facts hold the source_chunk_id FK, so they must go first).
            await conn.execute("DELETE FROM facts WHERE source_doc_id = $1", parsed.doc_id)
            await conn.execute("DELETE FROM document_chunks WHERE doc_id = $1", parsed.doc_id)

            # 3) assign each unique fact to exactly one chunk (span → containing chunk,
            #    span-less table facts → first chunk) so the GiST facts_no_overlap
            #    exclusion never sees the same (subject, fact_key, policy_key) twice.
            fact_targets: dict[
                tuple[str, str, str | None, object], tuple[ExtractedFact, int]
            ] = {}
            for i, chunk in enumerate(chunks):
                for fact in facts or []:
                    key = (
                        _normalize_subject_key(fact.subject_key),
                        fact.fact_key,
                        fact.policy_key,
                        eff_from,
                    )
                    if key in fact_targets:
                        continue
                    if fact.span is not None and fact.span not in chunk.text:
                        continue
                    fact_targets[key] = (fact, i)

            chunk_facts: dict[int, list[ExtractedFact]] = {}
            for fact, i in fact_targets.values():
                chunk_facts.setdefault(i, []).append(fact)

            # 4) document_chunks + facts + chunk_fact_refs
            for i, (chunk, cid) in enumerate(zip(chunks, chunk_ids)):
                await conn.execute(
                    """
                    INSERT INTO document_chunks (doc_id, chunk_id, chunk_index, content, text_hash, section)
                    VALUES ($1,$2,$3,$4,$5,$6)
                    ON CONFLICT (chunk_id) DO UPDATE
                      SET content = EXCLUDED.content, text_hash = EXCLUDED.text_hash
                    """,
                    parsed.doc_id, cid, i, chunk.text, _sha256(chunk.text), chunk.section_title,
                )
                for fact in chunk_facts.get(i, []):
                    subject_id = await _upsert_subject(conn, fact)
                    fact_id = await _insert_fact(
                        conn, fact, subject_id, parsed.doc_id, cid, eff_from, eff_to
                    )
                    fact_rows.append((fact_id, cid))

            for fact_id, cid in fact_rows:
                await conn.execute(
                    "INSERT INTO chunk_fact_refs (chunk_id, fact_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                    cid, fact_id,
                )

            # 5) ingest_log
            await conn.execute(
                """
                INSERT INTO ingest_log (doc_id, action, version, chunk_count, detail)
                VALUES ($1,'insert',$2,$3,$4)
                """,
                parsed.doc_id, version, len(chunks),
                f"facts={len(fact_rows)} kind={parsed.kind}",
            )
        # COMMIT tại exit transaction
    except Exception as exc:  # noqa: BLE001
        await conn.close()
        raise LoadError(f"load_document rollback (doc={parsed.doc_id}): {exc}") from exc

    # 6) ainsert SAU COMMIT — LightRAG (không block transaction)
    lightrag_doc_id: str | None = None
    try:
        from ingest.lightrag_init import ainsert_document, adelete_by_doc_id, get_lightrag

        rag = get_lightrag()
        if version > 1:
            # Re-ingest: LightRAG doc id equals doc_id for every version, so drop the
            # old doc first or its chunks linger alongside the new ones.
            await adelete_by_doc_id(rag, parsed.doc_id)
        # Annotate chunk text with ⟦FACT tokens (replaced by values at generation time).
        ainsert_texts = [
            replace_fact_with_placeholder(
                sanitize_forged_tokens(chunk.text),
                [(f.subject_key, f.fact_key, f.policy_key) for f in chunk_facts.get(i, [])],
            )[0]
            for i, chunk in enumerate(chunks)
        ]
        await ainsert_document(rag, parsed.doc_id, ainsert_texts, chunk_ids)
        lightrag_doc_id = parsed.doc_id  # ids/file_paths 1:1
    except Exception as exc:  # noqa: BLE001
        logger.warning("ainsert LightRAG lỗi (registry đã commit) — retry idempotent sau: %s", exc)

    await conn.close()
    return LoadResult(
        doc_id=parsed.doc_id,
        version=version,
        chunk_count=len(chunks),
        fact_count=len(fact_rows),
        lightrag_doc_id=lightrag_doc_id,
    )


async def expire_facts(subject_id: int, fact_key: str, policy_key: str | None, as_of) -> None:
    """Đóng interval facts hiện tại tại as_of (update giá/policy — plan §3.6)."""
    import datetime

    as_of = as_of or datetime.date.today()
    conn = await asyncpg.connect(settings.pg_dsn)
    try:
        await conn.execute(
            """
            UPDATE facts SET effective_to = $1
            WHERE subject_id = $2 AND fact_key = $3
              AND ($4::text IS NULL OR policy_key = $4)
              AND (effective_to IS NULL OR effective_to > $1)
            """,
            as_of, subject_id, fact_key, policy_key,
        )
    finally:
        await conn.close()


def _effective_from(parsed: ParsedDoc):
    """Ngày hiệu lực từ metadata docs — MVP: hôm nay nếu không set."""
    import datetime

    return getattr(parsed, "effective_from", None) or datetime.date.today()


def _effective_to(parsed: ParsedDoc):
    return getattr(parsed, "effective_to", None) or None


def _fallback_doc_id(path: str, kind: str) -> str:
    """doc_id derived from the filename, mirroring parser.parse_document."""
    import pathlib

    stem = pathlib.Path(path).stem.lower().replace(" ", "-")
    return f"{kind}-{stem}"


async def _ingest_dir(docs_dir: str, changed: str, kind: str) -> int:
    """Parse → extract → load every changed file under docs_dir; 1 on any failure."""
    import pathlib

    changed_ids = {c.strip() for c in changed.split(",") if c.strip()}
    exit_code = 0
    for p in sorted(pathlib.Path(docs_dir).iterdir()):
        if not p.is_file():
            continue
        if changed_ids and _fallback_doc_id(str(p), kind) not in changed_ids:
            continue
        try:
            parsed = await parse_document(str(p), kind)
            facts = None
            try:
                facts = await extract_facts(parsed.full_text, parsed.doc_id, parsed.kind)
            except Exception as exc:  # noqa: BLE001 — chunks stay indexable without facts
                logger.warning(
                    "fact extraction failed (doc=%s) — loading chunks only: %s",
                    parsed.doc_id, exc,
                )
            result = await load_document(parsed, facts)
            print(
                f"ingested {parsed.doc_id} v{result.version} "
                f"chunks={result.chunk_count} facts={result.fact_count} "
                f"lightrag={result.lightrag_doc_id}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("ingest failed (%s): %s", p, exc)
            exit_code = 1
    return exit_code


def main() -> int:
    """CLI: parse → extract → load for the changed docs (update_6mo.sh step 2)."""
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description="Ingest changed docs into the registry + LightRAG.")
    ap.add_argument("--dir", required=True, help="Directory containing the source docs.")
    ap.add_argument(
        "--changed",
        default="",
        help="Comma-separated doc_ids to ingest; empty = every file under --dir.",
    )
    ap.add_argument("--kind", choices=("legal", "price", "project"), default="legal")
    args = ap.parse_args()
    return asyncio.run(_ingest_dir(args.dir, args.changed, args.kind))


if __name__ == "__main__":
    raise SystemExit(main())
