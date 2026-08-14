-- rag-real-estate - Seed Camellia RUMOR price campaign (idempotent, UTF-8).
-- Run after db/camellia_estimate.sql.
--
-- All figures are estimates from the 2026-08-13 OCR price-guide slide +
-- feedback (ground_truth_patch.md); the official per-unit grid does not exist
-- yet -> every price fact is quality='range', trust_level='estimate', so
-- output confidence is capped at MEDIUM (ADR-0002 D6). CH-10/CH-11 have no
-- price fact.
--
-- Contents: price document + camellia-2026q3 campaign; 6 unit-type subjects,
-- CH-10/CH-11 (price_note only) and the project subject; 24 range price facts
-- (6 types x chuan/htls/thanh_thoi/som95); HTLS loan policy; HTLS banks.
-- Sources: data/_processed/price_matrix.json (ground-truth patched) +
-- payment_methods.json + business_rules.json.

BEGIN;

-- Price document carrying the rumor figure set.
INSERT INTO documents
  (doc_id, kind, title, source_file, effective_from, effective_to, status, content_hash, metadata)
VALUES
  ('price-camellia-2026q3', 'price', 'Giá định hướng Camellia Q3/2026 (RUMOR)',
   'data/_processed/price_matrix.json', '2026-08-13', NULL, 'published',
   encode(digest('seed:price-camellia-2026q3', 'sha256'), 'hex'),
   '{"project":"camellia","campaign":"camellia-2026q3","currency":"VND",
     "trust":"estimate","note":"Guidance bands per unit type x payment method; official grid pending."}')
ON CONFLICT (doc_id) DO NOTHING;

-- Price/policy facts must belong to a campaign (rule B7).
INSERT INTO campaigns
  (campaign_key, project_key, effective_from, effective_to, source_doc_id, status)
VALUES
  ('camellia-2026q3', 'camellia', '2026-08-13', NULL, 'price-camellia-2026q3', 'active')
ON CONFLICT (campaign_key) DO NOTHING;

-- Subjects: unit types, CH-10/CH-11 (price_note only, open #O2) and the project.
INSERT INTO fact_subjects (subject_key, subject_type, display_name, project_key, attrs)
VALUES
  ('unit:camellia/studio',       'unit', 'Căn hộ Studio', 'camellia',
   '{"type":"Studio","bedrooms":0,"area_m2":[27.8,31.4],"view":"nội khu",
     "units":["CH-09","CH-12A","CH-16","CH-22"],
     "floor_rule":"từ tầng 3A lên, +0.3-0.4%/tầng"}'),
  ('unit:camellia/1p1',          'unit', 'Căn hộ 1.5PN (1PN + 1)', 'camellia',
   '{"type":"1.5PN","bedrooms":1,"area_m2":[47.0,47.3],"view":"nội khu",
     "units":["CH-06","CH-12","CH-17","CH-21"],
     "floor_rule":"từ tầng 3A lên, +0.3-0.4%/tầng","units_note":"Dải diện tích tờ rơi 46.8 chưa xác nhận (open #O4)"}'),
  ('unit:camellia/2pn-noi-khu',  'unit', 'Căn hộ 2PN View nội khu', 'camellia',
   '{"type":"2PN","bedrooms":2,"area_m2":[57.4,72.1],"view":"nội khu",
     "units":["CH-15","CH-18","CH-10"], "units_note":"CH-10 chưa xác nhận giá (open #O2)",
     "floor_rule":"từ tầng 3A lên, +0.3-0.4%/tầng"}'),
  ('unit:camellia/2pn-mat-duong','unit', 'Căn hộ 2PN mặt đường Lê Đức Thọ / Lê Văn Lương', 'camellia',
   '{"type":"2PN","bedrooms":2,"area_m2":[65.1,69.9],"view":"mặt đường",
     "units":["CH-05","CH-03A","CH-01","CH-02"],
     "floor_rule":"từ tầng 3A lên, +0.3-0.4%/tầng"}'),
  ('unit:camellia/2pn-goc',      'unit', 'Căn hộ 2PN góc view núi Sơn Trà + biển', 'camellia',
   '{"type":"2PN","bedrooms":2,"area_m2":[70.1,72.7],"view":"góc núi + biển",
     "units":["CH-08","CH-12B","CH-07"], "units_note":"CH-08 tim tường chưa chốt nguồn (open #O1)",
     "floor_rule":"từ tầng 3A lên, +0.3-0.4%/tầng"}'),
  ('unit:camellia/3pn',          'unit', 'Căn hộ 3PN góc view biển', 'camellia',
   '{"type":"3PN","bedrooms":3,"area_m2":[84.2,103.6],"view":"góc biển",
     "units":["CH-03","CH-19","CH-20","CH-11"], "units_note":"CH-11 chưa xác nhận giá (open #O2)",
     "floor_rule":"từ tầng 3A lên, +0.3-0.4%/tầng"}'),
  -- CH-10 / CH-11: no price fact - alias resolving to their unit-type band
  -- via attrs.unit_type_key so the affordability leg (3.1) returns the group
  -- band + has_approx instead of inventing a per-unit price (Plan-check M2).
  ('unit:camellia/CH-10', 'unit', 'Căn CH-10 (2PN, 61.7 m², 2VS)', 'camellia',
   '{"price_note":"Chưa có giá chính thức (pending_confirm, open #O2)","area_m2":61.7,"tim_tuong_m2":66.9,"unit_type_key":"unit:camellia/2pn-noi-khu"}'),
  ('unit:camellia/CH-11', 'unit', 'Căn CH-11 (3PN, 88.7 m², 3VS)', 'camellia',
   '{"price_note":"Chưa có giá chính thức (pending_confirm, open #O2)","area_m2":88.7,"tim_tuong_m2":97.9,"unit_type_key":"unit:camellia/3pn"}'),
  ('project:camellia', 'project', 'The Camellia Son Tra - Da Nang', 'camellia',
   '{"total_units":469,"tmds":10,"handover":"Q1/2028","trust":"rumor",
     "developer":"Công ty TNHH Địa ốc Thành Lâm","location":"Giao lộ Lê Văn Lương - Lê Đức Thọ, Sơn Trà, Đà Nẵng"}')
ON CONFLICT (subject_key) DO NOTHING;

-- 24 RANGE price facts (6 types x chuan/htls/thanh_thoi/som95).
-- Values are VND billion converted to exact VND (NUMERIC(20,0), AD-14).
-- quality='range', trust_level='estimate', volatile (awaiting official grid).
INSERT INTO facts
  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,
   range_min, range_max, volatile, effective_from, effective_to, source_doc_id,
   extract_conf, trust_level)
SELECT s.id, 'price_vnd', p.policy_key, 'camellia-2026q3', NULL, 'vnd', 'range',
       p.range_min::NUMERIC(20,0), p.range_max::NUMERIC(20,0), TRUE,
       '2026-08-13', NULL, 'price-camellia-2026q3', 0.60, 'estimate'
FROM (VALUES
  -- Studio
  ('unit:camellia/studio', 'chuan',      1900000000, 2530000000),
  ('unit:camellia/studio', 'htls',       1980000000, 2640000000),
  ('unit:camellia/studio', 'thanh_thoi', 1940000000, 2590000000),
  ('unit:camellia/studio', 'som95',      1720000000, 2300000000),
  -- 1.5PN
  ('unit:camellia/1p1', 'chuan',      3150000000, 3950000000),
  ('unit:camellia/1p1', 'htls',       3280000000, 4110000000),
  ('unit:camellia/1p1', 'thanh_thoi', 3210000000, 4030000000),
  ('unit:camellia/1p1', 'som95',      2850000000, 3580000000),
  -- 2PN inner view
  ('unit:camellia/2pn-noi-khu', 'chuan',      3740000000, 4790000000),
  ('unit:camellia/2pn-noi-khu', 'htls',       3900000000, 4990000000),
  ('unit:camellia/2pn-noi-khu', 'thanh_thoi', 3820000000, 4890000000),
  ('unit:camellia/2pn-noi-khu', 'som95',      3390000000, 4340000000),
  -- 2PN street
  ('unit:camellia/2pn-mat-duong', 'chuan',      4310000000, 5160000000),
  ('unit:camellia/2pn-mat-duong', 'htls',       4490000000, 5370000000),
  ('unit:camellia/2pn-mat-duong', 'thanh_thoi', 4400000000, 5260000000),
  ('unit:camellia/2pn-mat-duong', 'som95',      3910000000, 4670000000),
  -- 2PN corner sea/mountain
  ('unit:camellia/2pn-goc', 'chuan',      4440000000, 5750000000),
  ('unit:camellia/2pn-goc', 'htls',       4630000000, 5990000000),
  ('unit:camellia/2pn-goc', 'thanh_thoi', 4540000000, 5870000000),
  ('unit:camellia/2pn-goc', 'som95',      4030000000, 5210000000),
  -- 3PN corner sea view
  ('unit:camellia/3pn', 'chuan',      7200000000, 8630000000),
  ('unit:camellia/3pn', 'htls',       7500000000, 8990000000),
  ('unit:camellia/3pn', 'thanh_thoi', 7350000000, 8810000000),
  ('unit:camellia/3pn', 'som95',      6530000000, 7820000000)
) AS p(subject_key, policy_key, range_min, range_max)
JOIN fact_subjects s ON s.subject_key = p.subject_key
WHERE NOT EXISTS (
  SELECT 1 FROM facts f
  WHERE f.subject_id = s.id AND f.fact_key = 'price_vnd'
    AND COALESCE(f.policy_key, '') = p.policy_key
    AND f.effective_from = '2026-08-13'
);

-- HTLS loan policy for all 6 unit types (policy_key='htls' only): max 70% loan
-- -> 30% deposit; 0% interest for the first 18 months; principal grace up to 5
-- years (feedback Table 6 #4). Deposit 30% is DERIVED from the max-70%-loan
-- figure whose source is partial_confirmed (open #O8) -> trust 'estimate';
-- term 18 and 0% interest are confirmed by feedback. interest 0.0000 = true 0%
-- (HTLS); NULL elsewhere means n/a, not 0% (D6). MBV pending (open #O9).
INSERT INTO facts
  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,
   volatile, effective_from, effective_to, source_doc_id, extract_conf, trust_level)
SELECT s.id, t.fact_key, 'htls', 'camellia-2026q3', t.value_num, t.unit, 'exact',
       FALSE, '2026-08-13', NULL, 'price-camellia-2026q3', 0.90,
       CASE WHEN t.fact_key = 'deposit_pct' THEN 'estimate' ELSE 'confirmed' END
FROM fact_subjects s
CROSS JOIN (VALUES
  ('deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('term_months',        18::NUMERIC,         'months'),
  ('interest_rate_pct',  0.0000::NUMERIC(6,4), 'pct')
) AS t(fact_key, value_num, unit)
WHERE s.subject_type = 'unit'
  AND s.subject_key IN ('unit:camellia/studio', 'unit:camellia/1p1',
                        'unit:camellia/2pn-noi-khu', 'unit:camellia/2pn-mat-duong',
                        'unit:camellia/2pn-goc', 'unit:camellia/3pn')
  AND NOT EXISTS (
    SELECT 1 FROM facts f
    WHERE f.subject_id = s.id AND f.fact_key = t.fact_key
      AND COALESCE(f.policy_key, '') = 'htls'
      AND f.effective_from = '2026-08-13'
  );

-- HTLS banks on the project subject: VietinBank/MB/SHB confirmed; MBV pending
-- (open #O9), hence approx/estimate.
INSERT INTO facts
  (subject_id, fact_key, policy_key, campaign_key, value_text, unit, quality,
   volatile, effective_from, effective_to, source_doc_id, extract_conf, trust_level)
SELECT s.id, 'htls_banks', 'htls', 'camellia-2026q3', v.value_text, 'enum', v.quality,
       FALSE, '2026-08-13', NULL, 'price-camellia-2026q3', 0.90, v.trust
FROM fact_subjects s
CROSS JOIN (VALUES
  ('VietinBank, MB Bank, SHB (MBV dự kiến - chưa xác nhận)', 'approx', 'estimate')
) AS v(value_text, quality, trust)
WHERE s.subject_key = 'project:camellia'
  AND NOT EXISTS (
    SELECT 1 FROM facts f
    WHERE f.subject_id = s.id AND f.fact_key = 'htls_banks'
      AND f.value_text = v.value_text
  );

COMMIT;