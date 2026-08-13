-- rag-real-estate — Seed: loan policies (policy matrix -> facts). Plan §3.3 + §3.4 (edge case NULL != 0%).
-- MOCK/DEMO DATA: sample policies, awaiting real data (plan §10); not for production.
-- Inserts policy facts (deposit_pct/term_months/interest_rate_pct) for EVERY unit with price_vnd, with
-- campaign_key + policy_key; safe alongside db/seed/price_campaigns.sql (NOT EXISTS guards keyed on subject/fact/policy/from).
-- Matrix: bank_a 25.00%/180mo/8.5000% | bank_b 30.00%/240mo/8.0000% | support 0.00%/120mo/0.0000% (A06-01 only).
-- Exclusions: A06-01 takes only 'support' (0% down instead of a bank); A04-03 none -> absent from v_unit_offers. Numeric discipline per AD-14. Idempotent | UTF-8.

BEGIN;

-- 1. bank_a / bank_b for every unit with price_vnd (except A06-01, A04-03)
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
-- Every unit with a price fact (interval + campaign derive from the current price fact)
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

-- 2. support (0% down payment) — applied only to A06-01
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
