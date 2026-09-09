-- Repair Soleil LightRAG scope metadata without re-embedding healthy vectors.
-- Only rows whose current ids identify Soleil are touched; current/versioned ids remain unchanged.
BEGIN;

-- Registry rows are the authoritative project scope. Do not broaden this predicate.
UPDATE documents
SET project_key = 'soleil', updated_at = now()
WHERE doc_id LIKE '%-soleil-%'
  AND project_key IS DISTINCT FROM 'soleil';

-- LightRAG stores have no registry FK. A LightRAG raw chunk is either the
-- registry chunk id or its generated -chunk-* child id.
UPDATE lightrag_doc_chunks c
SET workspace = 'ragre_mvp', update_time = now()
WHERE c.id LIKE '%-soleil-%'
  AND c.workspace IS DISTINCT FROM 'ragre_mvp';

UPDATE lightrag_vdb_chunks_text_embedding_v4_1024d v
SET workspace = 'ragre_mvp', update_time = now()
WHERE v.id LIKE '%-soleil-%'
  AND v.workspace IS DISTINCT FROM 'ragre_mvp';

-- Only a complete current document may transition a failed status. Completeness
-- requires every registry chunk to have a LightRAG status, raw chunk, and
-- workspace-scoped vector(1024). Incomplete failed rows remain failed.
WITH current_chunks AS (
  SELECT d.doc_id, c.chunk_id
  FROM documents d
  JOIN document_chunks c ON c.doc_id = d.doc_id
  WHERE d.project_key = 'soleil'
    AND d.doc_id LIKE '%-soleil-%'
    AND c.chunk_id LIKE '%-soleil-%'
), complete_docs AS (
  SELECT cc.doc_id
  FROM current_chunks cc
  GROUP BY cc.doc_id
  HAVING count(*) = count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM lightrag_doc_status s
           WHERE s.id = cc.chunk_id OR s.id LIKE cc.chunk_id || '-chunk-%'
         ))
     AND count(*) = count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM lightrag_doc_chunks c
           WHERE c.id = cc.chunk_id OR c.id LIKE cc.chunk_id || '-chunk-%'
         ))
     AND count(*) = count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM lightrag_vdb_chunks_text_embedding_v4_1024d v
           WHERE (v.id = cc.chunk_id OR v.id LIKE cc.chunk_id || '-chunk-%')
             AND v.workspace = 'ragre_mvp'
             AND vector_dims(v.content_vector) = 1024
         ))
)
UPDATE lightrag_doc_status s
SET workspace = 'ragre_mvp',
    metadata = jsonb_set(COALESCE(s.metadata, '{}'::jsonb), '{project_key}', '"soleil"'::jsonb, true),
    status = 'processed',
    updated_at = now()
WHERE s.id LIKE '%-soleil-%'
  AND s.status = 'failed'
  AND EXISTS (
    SELECT 1 FROM current_chunks cc
    JOIN complete_docs cd ON cd.doc_id = cc.doc_id
    WHERE s.id = cc.chunk_id OR s.id LIKE cc.chunk_id || '-chunk-%'
  );

-- Scope metadata can be repaired independently, but status validity is checked
-- against current registry rows below. Any failure aborts the transaction.
UPDATE lightrag_doc_status s
SET workspace = 'ragre_mvp',
    metadata = jsonb_set(COALESCE(s.metadata, '{}'::jsonb), '{project_key}', '"soleil"'::jsonb, true),
    updated_at = now()
WHERE s.id LIKE '%-soleil-%'
  AND (s.workspace IS DISTINCT FROM 'ragre_mvp'
       OR s.metadata->>'project_key' IS DISTINCT FROM 'soleil');

DO $$
DECLARE bad bigint;
BEGIN
  SELECT count(*) INTO bad
  FROM (
    SELECT d.doc_id
    FROM documents d
    JOIN document_chunks c ON c.doc_id = d.doc_id
    WHERE d.project_key = 'soleil' AND d.doc_id LIKE '%-soleil-%'
    GROUP BY d.doc_id
    HAVING count(*) <> count(*) FILTER (WHERE EXISTS (
      SELECT 1 FROM lightrag_doc_status s
      WHERE s.id = c.chunk_id OR s.id LIKE c.chunk_id || '-chunk-%'
    ))
    OR count(*) <> count(*) FILTER (WHERE EXISTS (
      SELECT 1 FROM lightrag_doc_chunks lc
      WHERE lc.id = c.chunk_id OR lc.id LIKE c.chunk_id || '-chunk-%'
    ))
    OR count(*) <> count(*) FILTER (WHERE EXISTS (
      SELECT 1 FROM lightrag_vdb_chunks_text_embedding_v4_1024d v
      WHERE (v.id = c.chunk_id OR v.id LIKE c.chunk_id || '-chunk-%')
        AND v.workspace = 'ragre_mvp' AND vector_dims(v.content_vector) = 1024
    ))
  ) incomplete;
  IF bad > 0 THEN RAISE EXCEPTION 'Soleil LightRAG document parity incomplete: % document(s)', bad; END IF;

  SELECT count(*) INTO bad
  FROM lightrag_doc_status s
  WHERE s.id LIKE '%-soleil-%'
    AND (s.workspace IS DISTINCT FROM 'ragre_mvp'
         OR s.metadata->>'project_key' IS DISTINCT FROM 'soleil'
         OR s.status <> 'processed');
  IF bad > 0 THEN RAISE EXCEPTION 'Soleil LightRAG statuses remain invalid: %', bad; END IF;
END $$;

COMMIT;
