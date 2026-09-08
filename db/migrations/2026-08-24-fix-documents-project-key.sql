-- rag-real-estate — Migration 2026-08-24: re-tag documents.project_key for the
-- Soleil/Camellia corpus (project-scoped retrieval isolation, story 10.4).
--
-- Why: the 2026-08-21 migration backfilled project_key only from the campaigns
-- join (source_doc_id), so every non-campaign document — including ALL Soleil
-- sales-policy/payment/legal docs and the entire Camellia legal corpus — fell
-- through to the reserved '_legacy' key. The retrieval post-filter
-- (rag_leg._post_filter) and SQL-leg predicates require `project_key = <p>`,
-- so '_legacy' docs are invisible to every project-scoped query: a Soleil
-- "chính sách bán hàng" question lost its Soleil grounding while the shared
-- LightRAG store still surfaced Camellia chunks (ungrounded LOW answers +
-- cross-project contamination).
--
-- What this migration does (idempotent, safe to re-run):
--   1. docs still on '_legacy' whose doc_id carries a project token as its
--      second token (`<kind>-<project>-<slug>`) are re-tagged with that token;
--   2. the Camellia-SPECIFIC legal corpus (no project token in the doc_id) is
--      re-tagged 'camellia' by an explicit allowlist;
--   3. wrong tags are corrected: any active-project doc whose current key
--      differs from the doc_id convention/allowlist is re-tagged (reserved
--      namespaces '_legacy'/'_training' are never touched);
--   4. everything still unmatched stays '_legacy' and is re-queued for human
--      review (never guessed) — this deliberately includes the shared/national
--      law docs (ldd-2013, nd-101-2024, blds-2015, ...): they are shared
--      knowledge, NOT Camellia corpus, so assigning them to 'camellia' would
--      hide them from every other project;
--   5. an ASSERTION fails the transaction if ANY active-project doc is left
--      unresolved ('_legacy'/NULL) or mismatched — scoped to the Soleil/Camellia
--      corpus only, so shared/national docs are not forced into a project to
--      make the count zero.
--
-- Rollback: run `UPDATE documents SET project_key = '_legacy' WHERE doc_id IN
-- (...)` with the inverse set from the review_queue payload; the migration is
-- idempotent and safe to re-run.

BEGIN;

-- 1. Convention-tagged docs: second token of `<kind>-<project>-<slug>`.
--    Matches every Soleil/Camellia/tower doc currently stuck on '_legacy'
--    (or NULL on a fresh schema that skipped the 2026-08-21 backfill).
UPDATE documents
SET project_key = split_part(doc_id, '-', 2)
WHERE COALESCE(project_key, '_legacy') = '_legacy'
  AND split_part(doc_id, '-', 2) IN ('soleil', 'camellia', 'tower-a', 'tower-b');

-- 1b. Multi-token project keys that never appear as the second token
--     (`project-tower-a` -> tokens project/tower/a): explicit mapping.
UPDATE documents
SET project_key = 'tower-a'
WHERE COALESCE(project_key, '_legacy') = '_legacy' AND doc_id = 'project-tower-a';

UPDATE documents
SET project_key = 'tower-b'
WHERE COALESCE(project_key, '_legacy') = '_legacy' AND doc_id = 'project-tower-b';

-- 2. Camellia-SPECIFIC legal corpus: the four legal texts in the Camellia
--    builder (ingest/camellia_docs.py _legal_docs) carry no project token in
--    their doc_id, but document THE Camellia land/permits — these are the docs
--    Soleil must never see. National/shared law docs (ldd-2013, nd-101-2024,
--    blds-2015, ...) are deliberately NOT listed: assigning them would
--    mis-scope shared knowledge into one project (reviewer 10.4).
UPDATE documents
SET project_key = 'camellia'
WHERE COALESCE(project_key, '_legacy') = '_legacy'
  AND doc_id IN (
    'legal-gcnqsd-2011',
    'legal-qd254-2024',
    'legal-qd191-2025',
    'legal-cv12779-2026'
  );

-- 3. Correct wrong tags (idempotent): any active-project doc whose current key
--    differs from the doc_id convention/allowlist is re-tagged. Reserved
--    namespaces ('_training', ...) are never overwritten, and docs outside the
--    active-project set (national/shared) are never touched.
UPDATE documents d
SET project_key = e.expected
FROM (
  SELECT doc_id,
         CASE
           WHEN split_part(doc_id, '-', 2) IN ('soleil', 'camellia')
             THEN split_part(doc_id, '-', 2)
           WHEN doc_id IN ('legal-gcnqsd-2011', 'legal-qd254-2024',
                           'legal-qd191-2025', 'legal-cv12779-2026')
             THEN 'camellia'
           ELSE NULL
         END AS expected
  FROM documents
) e
WHERE d.doc_id = e.doc_id
  AND e.expected IS NOT NULL
  AND d.project_key IS DISTINCT FROM e.expected
  AND COALESCE(d.project_key, '') NOT LIKE '\_%';

-- 4. Re-queue any document still on '_legacy' so a human tags it (never guess).
INSERT INTO review_queue (kind, doc_id, payload, status)
SELECT 'fact_extract', doc_id,
       jsonb_build_object('reason', 'documents.project_key migration 2026-08-24: doc_id not mappable -> _legacy',
                          'migration', '2026-08-24-fix-documents-project-key'),
       'open'
FROM documents
WHERE COALESCE(project_key, '_legacy') = '_legacy'
  AND NOT EXISTS (
    SELECT 1 FROM review_queue rq
    WHERE rq.doc_id = documents.doc_id
      AND rq.payload->>'migration' = '2026-08-24-fix-documents-project-key'
  );

-- 5. Assertion: zero unresolved/mismatched ACTIVE-PROJECT docs. Scoped to the
--    Soleil/Camellia corpus (doc_id convention + Camellia legal allowlist);
--    shared/national docs are not assigned and not counted, so this can never
--    be satisfied by force-tagging them. Fails the transaction if violated.
DO $$
DECLARE
  n_bad INT;
BEGIN
  SELECT count(*) INTO n_bad
  FROM documents d
  WHERE (
          split_part(d.doc_id, '-', 2) IN ('soleil', 'camellia')
          OR d.doc_id IN ('legal-gcnqsd-2011', 'legal-qd254-2024',
                          'legal-qd191-2025', 'legal-cv12779-2026')
        )
    AND COALESCE(d.project_key, '') NOT LIKE '\_%'
    AND d.project_key IS DISTINCT FROM (
          CASE
            WHEN split_part(d.doc_id, '-', 2) IN ('soleil', 'camellia')
              THEN split_part(d.doc_id, '-', 2)
            WHEN d.doc_id IN ('legal-gcnqsd-2011', 'legal-qd254-2024',
                              'legal-qd191-2025', 'legal-cv12779-2026')
              THEN 'camellia'
            ELSE NULL
          END
        );
  IF n_bad > 0 THEN
    RAISE EXCEPTION
      'documents.project_key migration: % active-project doc(s) unresolved or mismatched — fix before re-running',
      n_bad;
  END IF;
END
$$;

COMMIT;
