"""Run the ingest pipeline over the Soleil corpus (mirror of run_camellia_ingest.py).

Loads every document produced by ingest.soleil_docs.build_documents() through
ingest.load.load_document, then (as a separate step, after this driver reports
PASS) the integrity gates of scripts/verify_ingest.sql are run.

Seed-fact preservation: load_document true-replaces facts on re-ingest, which
would DELETE the unit/price/policy facts seeded for `price-soleil-2026q3` by
db/seed/soleil_campaign.sql. Those rows (source_chunk_id NULL) are the single
source of truth for the campaign figures, so for that one document we:
  * skip the LLM fact-extraction path, and
  * pass load_document(preserve_seed_facts=True), which only deletes rows this
    loader created itself and leaves the seed rows untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from ingest.fact_extract import extract_facts
from ingest.load import load_document
from ingest.soleil_docs import build_documents

logger = logging.getLogger(__name__)

# Seed-carrier documents: db/seed/soleil_campaign.sql owns their facts, so the
# loader must preserve them and never re-extract.
# rental-soleil-2026q3: its nightly rental figures live in the seed
# (db/seed/soleil_rental.sql) as source_chunk_id-NULL facts; LLM extraction of
# the enriched nightly-rate text could emit price_vnd rows that collide with
# the campaign seed (facts_no_overlap GiST), so extraction is skipped here.
PRESERVE_SEED_FACTS = {"price-soleil-2026q3", "rental-soleil-2026q3"}

# Project registry key for this corpus — every document row is tagged with it
# so project-scoped retrieval (rag_leg._post_filter, sql_leg facts/estimates)
# never mixes Soleil and Camellia chunks (story 10.4 isolation).
PROJECT_KEY = "soleil"


def plan_doc_ingest(doc_id: str) -> tuple[bool, bool]:
    """Return (extract_facts?, preserve_seed_facts?) for a registry doc_id."""
    return (False, True) if doc_id in PRESERVE_SEED_FACTS else (True, False)


async def _ingest_one(doc) -> str:
    """Extract + load a single document; returns a one-line report."""
    extract, preserve = plan_doc_ingest(doc.doc_id)
    facts = None
    ingest_status = "facts_loaded" if preserve else "extract_failed"
    if extract:
        try:
            facts = await extract_facts(doc.full_text, doc.doc_id, doc.kind)
            ingest_status = (
                "review_required"
                if any((f.extract_conf is not None and f.extract_conf < 0.6) for f in facts)
                else ("facts_loaded" if facts else "chunks_only")
            )
        except Exception as exc:  # noqa: BLE001 - chunks stay indexable without facts
            logger.warning(
                "fact extraction failed (doc=%s) - loading chunks only: %s",
                doc.doc_id,
                exc,
            )
    result = await load_document(
        doc,
        facts,
        preserve_seed_facts=preserve,
        project_key=PROJECT_KEY,
        ingest_status=ingest_status,
    )
    if result.lightrag_doc_id is None:
        raise RuntimeError(
            f"{doc.doc_id}: LightRAG ainsert did not complete (lightrag_doc_id=None)"
        )
    return (
        f"{result.doc_id} v{result.version} chunks={result.chunk_count} "
        f"facts={result.fact_count} extracted={len(facts or [])} "
        f"lightrag={result.lightrag_doc_id}"
    )


async def _run(verbose: bool = False, only: set[str] | None = None) -> int:
    docs = build_documents()
    if only:
        docs = [d for d in docs if d.doc_id in only]
        missing = only - {d.doc_id for d in docs}
        if missing:
            print(f"unknown --only doc ids: {sorted(missing)}")
            return 2
    if verbose:
        for d in docs:
            extract, preserve = plan_doc_ingest(d.doc_id)
            print(f"[plan] {d.doc_id} kind={d.kind} extract={extract} preserve={preserve}")
    failures = 0
    for doc in docs:
        try:
            print(await _ingest_one(doc))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            logger.error("ingest failed (%s): %s", doc.doc_id, exc)
    if failures:
        print(
            f"\nINGEST: {failures}/{len(docs)} documents FAILED - "
            "sửa lỗi rồi chạy lại (idempotent, seed facts được bảo toàn)."
        )
        return 1
    print(f"\nINGEST: PASS - {len(docs)} documents. Tiếp theo: scripts/verify_ingest.sql.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the ingest pipeline over the Soleil registry.")
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print the per-doc plan (extract/preserve) before loading.",
    )
    ap.add_argument(
        "--only",
        default="",
        help="Comma-separated doc_ids to ingest (default: all) — retry without re-versioning healthy docs.",
    )
    args = ap.parse_args(argv)
    only = None
    if args.only:
        parsed_ids = {d.strip() for d in args.only.split(",") if d.strip()}
        if not parsed_ids:
            import sys
            print(
                "error: --only must be a comma-separated list of non-empty doc_ids (e.g. --only doc1,doc2)",
                file=sys.stderr,
            )
            return 2
        only = parsed_ids
    return asyncio.run(_run(args.verbose, only))


if __name__ == "__main__":
    raise SystemExit(main())
