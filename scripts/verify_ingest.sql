-- =============================================================================
-- rag-real-estate — scripts/verify_ingest.sql (A13 integrity gates)
-- Plan §10 Ngày 3-4 | Chạy: psql -f scripts/verify_ingest.sql
--
-- Mọi query trả về ROW COUNT PHẢI = 0. Nếu count > 0 → ingest có lỗi → FIX trước
-- khi đi tiếp. Output dạng: check_name | count | status (OK/FAIL).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- CHK-1: Dangling placeholder — chunk có '⟦FACT' nhưng số token mở ≠ số token đóng
-- (regex trên document_chunks.content; '⟦FACT' không có '⟧' tương ứng).
-- ---------------------------------------------------------------------------
WITH placeholder_balance AS (
  SELECT c.chunk_id,
    (SELECT count(*) FROM regexp_matches(c.content, '⟦FACT', 'g')) AS openers,
    (SELECT count(*) FROM regexp_matches(c.content, '⟧', 'g'))       AS closers
  FROM document_chunks c
)
SELECT 'chk1_dangling_placeholder' AS check_name,
       count(*)   AS count,
       CASE WHEN count(*) = 0 THEN 'OK' ELSE 'FAIL' END AS status
FROM placeholder_balance WHERE openers <> closers;

-- ---------------------------------------------------------------------------
-- CHK-2: Chunk có placeholder ⟦FACT nhưng KHÔNG có dòng chunk_fact_refs tương ứng
-- (placeholder-integrity §3.7: số token hoàn chỉnh == số dòng refs).
-- ---------------------------------------------------------------------------
SELECT 'chk2_placeholder_without_refs' AS check_name,
       count(*)   AS count,
       CASE WHEN count(*) = 0 THEN 'OK' ELSE 'FAIL' END AS status
FROM document_chunks c
WHERE c.content ~ '⟦FACT'
  AND NOT EXISTS (SELECT 1 FROM chunk_fact_refs r WHERE r.chunk_id = c.chunk_id);

-- ---------------------------------------------------------------------------
-- CHK-3: chunk_id format — phải khớp 'doc_id:version:index' (doc_id có thể chứa '-')
--   regex: <doc_id>:<version:int>:<index:int>
-- ---------------------------------------------------------------------------
SELECT 'chk3_chunk_id_format' AS check_name,
       count(*)   AS count,
       CASE WHEN count(*) = 0 THEN 'OK' ELSE 'FAIL' END AS status
FROM document_chunks
WHERE chunk_id !~ '^[a-z0-9][a-z0-9\-]*:[0-9]+:[0-9]+$';

-- ---------------------------------------------------------------------------
-- CHK-4: chk4a + chk4b — integrity refs (orphan chunk_fact_refs, fact trỏ chunk không tồn tại)
-- ---------------------------------------------------------------------------
SELECT 'chk4a_orphan_chunk_fact_refs' AS check_name,
       count(*)   AS count,
       CASE WHEN count(*) = 0 THEN 'OK' ELSE 'FAIL' END AS status
FROM chunk_fact_refs r
LEFT JOIN document_chunks c ON c.chunk_id = r.chunk_id
LEFT JOIN facts f ON f.id = r.fact_id
WHERE c.chunk_id IS NULL OR f.id IS NULL;

SELECT 'chk4b_fact_source_chunk_missing' AS check_name,
       count(*)   AS count,
       CASE WHEN count(*) = 0 THEN 'OK' ELSE 'FAIL' END AS status
FROM facts f
WHERE f.source_chunk_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM document_chunks c WHERE c.chunk_id = f.source_chunk_id);

-- ---------------------------------------------------------------------------
-- CHK-5 (bonus): interval chồng trong facts — nếu exclusion constraint bị tắt
-- hoặc dữ liệu seed lỗi, sẽ có 2 fact cùng (subject, fact_key, policy_key) đè nhau.
-- ---------------------------------------------------------------------------
SELECT 'chk5_fact_interval_overlap' AS check_name,
       count(*)   AS count,
       CASE WHEN count(*) = 0 THEN 'OK' ELSE 'FAIL' END AS status
FROM facts a
JOIN facts b ON b.id <> a.id
  AND b.subject_id = a.subject_id
  AND b.fact_key = a.fact_key
  AND COALESCE(b.policy_key, '') = COALESCE(a.policy_key, '')
  AND daterange(a.effective_from, COALESCE(a.effective_to, 'infinity'::date), '[)')
      && daterange(b.effective_from, COALESCE(b.effective_to, 'infinity'::date), '[)');

-- ---------------------------------------------------------------------------
-- CHK-6 (bonus): v_unit_offers không được chứa căn KHÔNG policy (A04-03) và
-- KHÔNG được chứa giá expired (tower-b). 2 câu hỏi riêng:
-- ---------------------------------------------------------------------------
SELECT 'chk6a_no_policy_unit_in_offers' AS check_name,
       count(*)   AS count,
       CASE WHEN count(*) = 0 THEN 'OK' ELSE 'FAIL' END AS status
FROM v_unit_offers o
JOIN fact_subjects s ON s.id = o.subject_id
WHERE s.subject_key = 'unit:tower-a/A04-03';

SELECT 'chk6b_expired_campaign_in_offers' AS check_name,
       count(*)   AS count,
       CASE WHEN count(*) = 0 THEN 'OK' ELSE 'FAIL' END AS status
FROM v_unit_offers o
JOIN fact_subjects s ON s.id = o.subject_id
WHERE s.subject_key LIKE 'unit:tower-b/%';

-- ---------------------------------------------------------------------------
-- CHK-6c (bonus): v_unit_offers_as_of must bind as_of — at a historical date
-- inside tower-b's interval (2026-01-01..2026-03-31) tower-b units must appear,
-- unlike CHK-6b at CURRENT_DATE. Counts >0 only when the function drops as_of.
-- ---------------------------------------------------------------------------
SELECT 'chk6c_as_of_binds_historical' AS check_name,
       count(*)   AS count,
       CASE WHEN count(*) = 0 THEN 'OK' ELSE 'FAIL' END AS status
FROM (
  SELECT count(*) AS n
  FROM v_unit_offers_as_of('2026-02-01') o
  JOIN fact_subjects s ON s.id = o.subject_id
  WHERE s.subject_key LIKE 'unit:tower-b/%'
) t WHERE t.n = 0;

-- ---------------------------------------------------------------------------
-- CHK-7 (bonus): facts tích cực — số lượng policy đủ 3 fact/căn cho căn có policy
-- (deposit_pct + term_months + interest_rate_pct hiện hành).
-- ---------------------------------------------------------------------------
SELECT 'chk7_missing_policy_fact_for_policy_units' AS check_name,
       count(*)   AS count,
       CASE WHEN count(*) = 0 THEN 'OK' ELSE 'FAIL' END AS status
FROM (
  -- mỗi (unit, policy_key) hiện hành PHẢI đủ 3 fact: deposit_pct + term_months + interest_rate_pct
  SELECT s.subject_key, f.policy_key, count(*) AS n
  FROM facts f
  JOIN fact_subjects s ON s.id = f.subject_id
  WHERE f.policy_key IS NOT NULL
    AND f.effective_from <= CURRENT_DATE
    AND (f.effective_to IS NULL OR f.effective_to > CURRENT_DATE)
  GROUP BY s.subject_key, f.policy_key
  HAVING count(*) <> 3
) bad;
