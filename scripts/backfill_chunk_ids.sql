-- Generic chunk-id consistency repair (ISSUE-21-BE2 / ISSUE-33-ENV5).
-- Before the write section, export the PROBE and collision reports outside git.
-- This script changes only chunk_id; content, hashes, and embeddings are untouched.
-- Every operator input is a PostgreSQL parameter (never string-interpolated):
-- $1::text = project key; $2::text = optional expected-id prefix (NULL = all).
-- The derived id is <doc_id>:<version>:<chunk_index>.

-- PROBE (read-only). Review this result before executing the write section.
WITH candidates AS (
  SELECT c.id, c.doc_id, c.chunk_id, d.project_key, d.version, c.chunk_index,
         format('%s:%s:%s', c.doc_id, d.version, c.chunk_index) AS expected_chunk_id
  FROM document_chunks AS c
  JOIN documents AS d ON d.doc_id = c.doc_id
  WHERE d.project_key = $1::text
    AND ($2::text IS NULL OR format('%s:%s:%s', c.doc_id, d.version, c.chunk_index) LIKE $2::text || '%')
)
SELECT * FROM candidates
WHERE chunk_id IS DISTINCT FROM expected_chunk_id
ORDER BY doc_id, chunk_index;

-- Collision report. Non-zero rows must be resolved before any write.
WITH candidates AS (
  SELECT c.id, format('%s:%s:%s', c.doc_id, d.version, c.chunk_index) AS expected_chunk_id
  FROM document_chunks AS c JOIN documents AS d ON d.doc_id = c.doc_id
  WHERE d.project_key = $1::text
    AND ($2::text IS NULL OR format('%s:%s:%s', c.doc_id, d.version, c.chunk_index) LIKE $2::text || '%')
)
SELECT c.id AS source_id, c.expected_chunk_id, existing.id AS conflicting_id
FROM candidates AS c
JOIN document_chunks AS existing ON existing.chunk_id = c.expected_chunk_id
WHERE existing.id <> c.id;

SELECT count(*) AS orphan_count
FROM document_chunks AS c JOIN documents AS d ON d.doc_id = c.doc_id
WHERE d.project_key = $1::text
  AND c.chunk_id IS DISTINCT FROM format('%s:%s:%s', c.doc_id, d.version, c.chunk_index)
  AND ($2::text IS NULL OR format('%s:%s:%s', c.doc_id, d.version, c.chunk_index) LIKE $2::text || '%');

-- WRITE (run only after reviewing PROBE and confirming collision_count = 0).
BEGIN;
WITH candidates AS (
  SELECT c.id, format('%s:%s:%s', c.doc_id, d.version, c.chunk_index) AS expected_chunk_id
  FROM document_chunks AS c JOIN documents AS d ON d.doc_id = c.doc_id
  WHERE d.project_key = $1::text
    AND ($2::text IS NULL OR format('%s:%s:%s', c.doc_id, d.version, c.chunk_index) LIKE $2::text || '%')
)
UPDATE document_chunks AS target
SET chunk_id = candidates.expected_chunk_id
FROM candidates
WHERE target.id = candidates.id
  AND target.chunk_id IS DISTINCT FROM candidates.expected_chunk_id
  AND NOT EXISTS (
    SELECT 1 FROM document_chunks AS conflict
    WHERE conflict.chunk_id = candidates.expected_chunk_id AND conflict.id <> target.id
  );

-- Post-write audit. Zero rows means all selected candidates were repaired.
WITH candidates AS (
  SELECT c.id, c.doc_id, c.chunk_id, d.project_key, d.version, c.chunk_index,
         format('%s:%s:%s', c.doc_id, d.version, c.chunk_index) AS expected_chunk_id
  FROM document_chunks AS c JOIN documents AS d ON d.doc_id = c.doc_id
  WHERE d.project_key = $1::text
    AND ($2::text IS NULL OR format('%s:%s:%s', c.doc_id, d.version, c.chunk_index) LIKE $2::text || '%')
)
SELECT * FROM candidates
WHERE chunk_id IS DISTINCT FROM expected_chunk_id
ORDER BY doc_id, chunk_index;
COMMIT;
