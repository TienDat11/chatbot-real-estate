"""Story 2.3 — run the ingest pipeline over the real Camellia corpus.

Loads every document produced by ingest.camellia_docs.build_documents() through
ingest.load.load_document, then (as a separate step, after this driver reports
PASS) the integrity gates of scripts/verify_ingest.sql are run — a PR is only
raised after the DB proves clean.

Transaction discipline (PITFALL asyncpg-transaction-rollback-explicit): the
registry write happens inside a per-document transaction and the LightRAG ainsert
runs strictly AFTER that document's COMMIT — ainsert is never attempted for a
document whose registry write rolled back. Each document is its own atomic unit
(that is what the merged load.py owns; a single cross-document transaction would
require re-architecting the already-merged loader), and re-runs are idempotent,
so partial progress is safe to resume.

Seed-fact preservation (PITFALL ragre-doc-registry-effective-validity):
load_document true-replaces facts on re-ingest, which would DELETE the 24 range
price facts + HTLS policy/banks seeded for `price-camellia-2026q3` by
db/seed/camellia_rumor.sql. Those rows (source_chunk_id NULL) are the single
source of truth for the campaign figures, so for that one document we:
  * skip the LLM fact-extraction path — extract_facts would re-insert the same
    (subject_id, fact_key, policy_key, [effective_from, ...)) key and trip the
    facts_no_overlap GiST exclusion (db/schema.sql), and
  * pass load_document(preserve_seed_facts=True), which only deletes rows this
    loader created itself and leaves the seed rows untouched.

All other documents extract facts with the cheap model (settings.llm_model_extract,
ADR-0002 [FIX-9]); a failed extraction degrades to a chunks-only load, so one bad
LLM call cannot block the registry. Re-running is idempotent: documents.version
bumps, chunks are replaced, and facts are replaced (or preserved) without
duplicates — LightRAG gets adelete_by_doc_id before ainsert on every version > 1.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from ingest.camellia_docs import build_documents
from ingest.fact_extract import extract_facts
from ingest.load import load_document

logger = logging.getLogger(__name__)

# Seed-carrier documents: db/seed/camellia_rumor.sql owns their facts, so the
# loader must preserve them and never re-extract. Exact set (not a wildcard) so a
# future seed for another document keeps the default true-replace behavior.
PRESERVE_SEED_FACTS = {"price-camellia-2026q3"}


def plan_doc_ingest(doc_id: str) -> tuple[bool, bool]:
    """Return (extract_facts?, preserve_seed_facts?) for a registry doc_id."""
    return (False, True) if doc_id in PRESERVE_SEED_FACTS else (True, False)


async def _ingest_one(doc) -> str:
    """Extract + load a single document; returns a one-line report."""
    extract, preserve = plan_doc_ingest(doc.doc_id)
    facts = None
    if extract:
        try:
            facts = await extract_facts(doc.full_text, doc.doc_id, doc.kind)
        except Exception as exc:  # noqa: BLE001 — chunks stay indexable without facts
            logger.warning(
                "fact extraction failed (doc=%s) — loading chunks only: %s",
                doc.doc_id, exc,
            )
    result = await load_document(doc, facts, preserve_seed_facts=preserve)
    if result.lightrag_doc_id is None:
        # load_document swallows ainsert/adelete errors and returns None here, so
        # the registry would be committed while the vector store stays empty and
        # verify_ingest.sql still passes — fail closed instead of a silent PASS.
        raise RuntimeError(
            f"{doc.doc_id}: LightRAG ainsert did not complete (lightrag_doc_id=None)"
        )
    return (
        f"{result.doc_id} v{result.version} chunks={result.chunk_count} "
        f"facts={result.fact_count} extracted={len(facts or [])} "
        f"lightrag={result.lightrag_doc_id}"
    )


async def _run(verbose: bool = False) -> int:
    docs = build_documents()
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
            f"\nINGEST: {failures}/{len(docs)} documents FAILED — "
            "sửa lỗi rồi chạy lại (idempotent, seed facts được bảo toàn)."
        )
        return 1
    print(f"\nINGEST: PASS — {len(docs)} documents. Tiếp theo: scripts/verify_ingest.sql.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the Story 2.3 ingest pipeline over the Camellia registry."
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print the per-doc plan (extract/preserve) before loading.",
    )
    args = ap.parse_args()
    return asyncio.run(_run(args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
