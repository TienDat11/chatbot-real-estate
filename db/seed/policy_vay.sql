-- =============================================================================
-- rag-real-estate — Seed: chính sách vay (ma trận policy → facts)
-- Plan §3.3 (policy_key) + §3.4 (v_unit_offers) + edge case §3.4 (NULL ≠ 0%)
-- Idempotent | UTF-8
--
-- ⚠️ MOCK/DEMO DATA — dữ liệu demo (policy dựng sẵn), chờ dữ liệu thật từ
--   chủ dữ liệu (plan §10). Chưa từng áp lên DB thật; KHÔNG dùng trong production.
--
-- File này INSERT policy facts (deposit_pct/term_months/interest_rate_pct) cho
-- MỌI căn có price_vnd, gắn campaign_key + policy_key. Chạy độc lập hoặc SONG
-- SONG với db/seed/price_campaigns.sql — an toàn vì mọi INSERT có NOT EXISTS
-- guard theo (subject_id, fact_key, policy_key, effective_from).
--
-- Ma trận:
--   bank_a  → deposit 25.00% / term 180 tháng / lãi 8.5000%/năm
--   bank_b  → deposit 30.00% / term 240 tháng / lãi 8.0000%/năm
--   support → deposit  0.00% / term 120 tháng / lãi 0.0000%/năm  (chỉ A06-01)
--
-- Loại trừ:
--   * unit:tower-a/A06-01 — CHỈ nhận policy support (0% trả trước thay vì ngân hàng)
--   * unit:tower-a/A04-03 — KHÔNG policy → không xuất hiện trong v_unit_offers
--
-- Kỷ luật kiểu số (AD-14): % điểm NUMERIC(5,2); lãi suất NUMERIC(6,4);
-- term_months NUMERIC nguyên. NULL ≠ 0.00.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. bank_a / bank_b cho mọi căn có price_vnd (trừ A06-01, A04-03)
-- ---------------------------------------------------------------------------
WITH policy_tpl AS (
  SELECT * FROM (VALUES
    ('bank_a', 'deposit_pct',       25.00::NUMERIC(5,2), 'pct'),
    ('bank_a', 'term_months',       180::NUMERIC,        'months'),
    ('bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
    ('bank_b', 'deposit_pct',       30.00::NUMERIC(5,2), 'pct'),
    ('bank_b', 'term_months',       240::NUMERIC,        'months'),
    ('bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct')
  ) AS t(policy_key, fact_key, value_num, unit)
),
-- Mọi căn có fact giá (lấy interval + campaign từ fact price hiện hành)
units AS (
  SELECT f.subject_id, fs.subject_key, f.campaign_key, f.effective_from, f.effective_to, f.source_doc_id
  FROM facts f
  JOIN fact_subjects fs ON fs.id = f.subject_id
  WHERE f.fact_key = 'price_vnd' AND f.quality = 'exact'
)
INSERT INTO facts
  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,
   volatile, effective_from, effective_to, source_doc_id, extract_conf)
SELECT u.subject_id, t.fact_key, t.policy_key, u.campaign_key, t.value_num, t.unit,
       'exact', FALSE, u.effective_from, u.effective_to, u.source_doc_id, 0.95
FROM units u
CROSS JOIN policy_tpl t
WHERE u.subject_key NOT IN ('unit:tower-a/A06-01', 'unit:tower-a/A04-03')
  AND NOT EXISTS (
    SELECT 1 FROM facts f
    WHERE f.subject_id = u.subject_id AND f.fact_key = t.fact_key
      AND COALESCE(f.policy_key, '') = t.policy_key
      AND f.effective_from = u.effective_from
  );

-- ---------------------------------------------------------------------------
-- 2. support (0% trả trước) — chỉ áp dụng cho A06-01
-- ---------------------------------------------------------------------------
WITH support_tpl AS (
  SELECT * FROM (VALUES
    ('support', 'deposit_pct',       0.00::NUMERIC(5,2), 'pct'),
    ('support', 'term_months',       120::NUMERIC,        'months'),
    ('support', 'interest_rate_pct', 0.0000::NUMERIC(6,4), 'pct')
  ) AS t(policy_key, fact_key, value_num, unit)
),
unit_a06 AS (
  SELECT f.subject_id, fs.subject_key, f.campaign_key, f.effective_from, f.effective_to, f.source_doc_id
  FROM facts f
  JOIN fact_subjects fs ON fs.id = f.subject_id
  WHERE fs.subject_key = 'unit:tower-a/A06-01' AND f.fact_key = 'price_vnd' AND f.quality = 'exact'
)
INSERT INTO facts
  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,
   volatile, effective_from, effective_to, source_doc_id, extract_conf)
SELECT u.subject_id, t.fact_key, t.policy_key, u.campaign_key, t.value_num, t.unit,
       'exact', FALSE, u.effective_from, u.effective_to, u.source_doc_id, 0.95
FROM unit_a06 u
CROSS JOIN support_tpl t
WHERE NOT EXISTS (
  SELECT 1 FROM facts f
  WHERE f.subject_id = u.subject_id AND f.fact_key = t.fact_key
    AND COALESCE(f.policy_key, '') = t.policy_key
    AND f.effective_from = u.effective_from
);

COMMIT;
