-- Keep derived exact offers from treating estimated prices as confirmed.
-- This migration requires db/camellia_estimate.sql, which adds facts.trust_level.
CREATE OR REPLACE FUNCTION v_unit_offers_as_of(as_of date)
RETURNS TABLE (
  subject_id bigint,
  policy_key text,
  price_vnd numeric(20,0),
  deposit_pct numeric(5,2),
  term_months integer,
  interest_rate_pct numeric(6,4),
  required_down_payment_vnd numeric(20,0),
  loan_amount_vnd numeric(20,0),
  monthly_principal_vnd numeric(20,0),
  monthly_interest_estimate_vnd numeric(20,0)
)
LANGUAGE sql
STABLE
AS $$
  WITH cur AS (
    SELECT subject_id, policy_key, fact_key, value_num FROM facts
    WHERE effective_from <= as_of
      AND (effective_to IS NULL OR effective_to > as_of)
      AND quality = 'exact'
      AND (fact_key <> 'price_vnd' OR trust_level = 'confirmed')
  )
  SELECT pol.subject_id, pol.policy_key,
    price.value_num::NUMERIC(20,0) AS price_vnd,
    dep.value_num::NUMERIC(5,2) AS deposit_pct,
    term.value_num::INTEGER AS term_months,
    int_pct.value_num::NUMERIC(6,4) AS interest_rate_pct,
    CEIL(price.value_num * dep.value_num / 100.0)::NUMERIC(20,0) AS required_down_payment_vnd,
    ROUND(price.value_num * (100.0 - dep.value_num) / 100.0, 0)::NUMERIC(20,0) AS loan_amount_vnd,
    ROUND((price.value_num * (100.0 - dep.value_num) / 100.0) / NULLIF(term.value_num, 0), 0)::NUMERIC(20,0) AS monthly_principal_vnd,
    ROUND((price.value_num * (100.0 - dep.value_num) / 100.0) * int_pct.value_num / 100.0 / 12.0, 0)::NUMERIC(20,0) AS monthly_interest_estimate_vnd
  FROM (SELECT DISTINCT subject_id, policy_key FROM cur WHERE policy_key IS NOT NULL) pol
  JOIN cur dep ON dep.subject_id = pol.subject_id AND dep.policy_key = pol.policy_key AND dep.fact_key = 'deposit_pct'
  JOIN cur term ON term.subject_id = pol.subject_id AND term.policy_key = pol.policy_key AND term.fact_key = 'term_months'
  LEFT JOIN cur int_pct ON int_pct.subject_id = pol.subject_id AND int_pct.policy_key = pol.policy_key AND int_pct.fact_key = 'interest_rate_pct'
  JOIN cur price ON price.subject_id = pol.subject_id AND price.fact_key = 'price_vnd'
$$;

-- Deployment prerequisite: apply only after trust_level exists; do not backfill
-- missing trust values as confirmed. The predicate deliberately fails closed.
