-- rag-real-estate - Camellia estimate schema extension (ADR-0002 D2).
-- PostgreSQL 16.6+. Run after db/schema.sql, before db/seed/camellia_rumor.sql.
--
-- Purpose: minimal schema so the affordability leg (Epic 3) can run on rumored
-- Camellia pricing - price bands per unit type x payment method, HTLS loan
-- policy, floor labels. No LLM-derived numbers; views filter by interval
-- validity and role (security_invoker=true, same as v_unit_offers).
--
-- Rules:
--  * Estimated price facts use quality 'range'/'approx' + trust_level='estimate'
--    -> guard_output caps confidence at MEDIUM (ADR-0002 D6), never HIGH.
--  * Numeric discipline (AD-14): vnd NUMERIC(20,0); pct NUMERIC(5,2); rate NUMERIC(6,4).
--  * NULL != 0: NULL interest_rate_pct means no loan policy; such facts are
--    excluded from the loan leg (ADR-0002 D6).

-- trust_level: distinguishes confirmed vs estimated figures.
ALTER TABLE facts
  ADD COLUMN IF NOT EXISTS trust_level TEXT NOT NULL DEFAULT 'confirmed'
  CHECK (trust_level IN ('confirmed', 'estimate'));

-- floor_labels: display names for sale floors (ADR open item #2, verified=false
-- until data owner confirms naming rules, e.g. whether 12A maps to 13).
-- floor_index = ordinal from the lowest sold floor (3A = 1).
-- price_calc uses ADR-0002 [FIX-2]: cumulative = pct * (index-1) * 25/21
-- (floor 25 = index 22 -> hits +7%/+10% per the offer sheet).
CREATE TABLE IF NOT EXISTS floor_labels (
  floor_index   INT PRIMARY KEY CHECK (floor_index >= 1),
  display_label TEXT NOT NULL,
  verified      BOOLEAN NOT NULL DEFAULT false,
  note          TEXT
);

-- 3A (index 1) displays as physical floor 4; floors 5..25 as (i+3).
INSERT INTO floor_labels (floor_index, display_label, verified, note)
SELECT i,
       CASE WHEN i = 1 THEN '3A'
            ELSE (i + 3)::TEXT END,
       FALSE,
       'Placeholder: sold from 3A (floor 4) to 25; 12A->13 naming unconfirmed'
FROM generate_series(1, 22) AS i
ON CONFLICT (floor_index) DO NOTHING;

-- v_unit_estimates: derived view per unit type x payment method where price is
-- a range/approx fact. Cash leg reads price_min/max_vnd; loan leg reads
-- deposit_pct + interest_rate_pct (NULL = no loan policy for that method).
CREATE OR REPLACE VIEW v_unit_estimates
WITH (security_invoker = true) AS
WITH cur AS (
  SELECT subject_id, policy_key, fact_key, value_num, range_min, range_max, quality
  FROM facts
  WHERE effective_to IS NULL
)
SELECT s.subject_key,
       s.display_name,
       s.project_key,
       s.attrs,
       pol.policy_key,
       pr.range_min::NUMERIC(20,0) AS price_min_vnd,
       pr.range_max::NUMERIC(20,0) AS price_max_vnd,
       pr.quality                  AS price_quality,
       dp.value_num::NUMERIC(5,2)  AS deposit_pct,
       tm.value_num::INTEGER       AS term_months,
       ir.value_num::NUMERIC(6,4)  AS interest_rate_pct
FROM fact_subjects s
JOIN (
  SELECT DISTINCT subject_id, policy_key
  FROM cur
  WHERE fact_key = 'price_vnd' AND quality IN ('range', 'approx') AND range_min IS NOT NULL
) pol ON pol.subject_id = s.id
JOIN cur pr
  ON pr.subject_id = s.id AND pr.fact_key = 'price_vnd'
 AND pr.policy_key = pol.policy_key AND pr.quality IN ('range', 'approx')
LEFT JOIN cur dp
  ON dp.subject_id = s.id AND dp.fact_key = 'deposit_pct'
 AND dp.policy_key = pol.policy_key AND dp.quality = 'exact'
LEFT JOIN cur tm
  ON tm.subject_id = s.id AND tm.fact_key = 'term_months'
 AND tm.policy_key = pol.policy_key AND tm.quality = 'exact'
LEFT JOIN cur ir
  ON ir.subject_id = s.id AND ir.fact_key = 'interest_rate_pct'
 AND ir.policy_key = pol.policy_key AND ir.quality = 'exact';

-- Read role can query estimates; RLS re-filters via security_invoker.
GRANT SELECT ON v_unit_estimates TO ro_query;