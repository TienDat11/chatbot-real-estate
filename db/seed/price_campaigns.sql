-- =============================================================================
-- rag-real-estate — Seed: bảng giá + hồ sơ dự án (2 campaign, 29 căn)
-- Plan §3.3 + §3.4 + §3.6 + §10 Ngày 3-4 | Idempotent | UTF-8
--
-- ⚠️ MOCK/DEMO DATA — dữ liệu demo (giá/campaign dựng sẵn), chờ dữ liệu thật từ
--   chủ dữ liệu (plan §10). Chưa từng áp lên DB thật; KHÔNG dùng trong production.
--
-- Nội dung:
--   * documents kind='price': price-tower-a-2026q2 (active), price-tower-b-2026q1 (expired)
--   * documents kind='project': project-tower-a, project-tower-b
--   * campaigns: tower-a-2026q2 [2026-04-01, NULL) ACTIVE | tower-b-2026q1 [2026-01-01, 2026-03-31) EXPIRED
--   * fact_subjects: 29 unit (tower-a 21 + tower-b 8) + 2 project subjects
--   * facts: price_vnd + area_m2 cho mọi căn; policy facts (bank_a/bank_b mỗi căn,
--     support 0% cho A06-01; A04-03 KHÔNG policy)
--
-- Kỷ luật kiểu số (AD-14): tiền NUMERIC(20,0) nguyên đồng; % NUMERIC(5,2);
-- lãi suất NUMERIC(6,4); m2 NUMERIC(10,2). NULL ≠ 0.00.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. documents — bảng giá + hồ sơ dự án
--    content_hash = placeholder sha256('seed:<doc_id>') — ingest thay bằng hash file thật.
-- ---------------------------------------------------------------------------
INSERT INTO documents
  (doc_id, kind, title, source_file, effective_from, effective_to, status, content_hash, metadata)
VALUES
  ('price-tower-a-2026q2', 'price', 'Bảng giá tòa A đợt 2/2026',
   'bang-gia-toa-A-2026Q2.xlsx', '2026-04-01', NULL, 'published',
   'd11421d3fe185e076695d40e7e1522fc080409d368769d25b5a430121645030a',
   '{"project":"Tower A","campaign":"Đợt mở bán 2/2026","currency":"VND","price_structure":"tran/goc"}'),
  ('price-tower-b-2026q1', 'price', 'Bảng giá tòa B đợt 1/2026',
   'bang-gia-toa-B-2026Q1.xlsx', '2026-01-01', '2026-03-31', 'published',
   '08a2563df1a931bd74c2898808523ef6eb31fe918d53ef189add995061fba015',
   '{"project":"Tower B","campaign":"Đợt mở bán 1/2026","currency":"VND","price_structure":"tran/goc"}'),
  ('project-tower-a', 'project', 'Hồ sơ dự án Tower A',
   'ho-so-du-an-tower-a.pdf', '2026-04-01', NULL, 'published',
   'd9b7b866fd7a32ff05b36749046b1fb728497d4a307bfd404fe3f5f42c0e607a',
   '{"project_name":"Tower A","location":"Quận Liên Chiểu, Đà Nẵng","developer":"Công ty BĐS số 1","total_units":200,"handover_date":"2027-12-31","amenities":["hồ bơi","gym","khu trẻ em"],"legal_status":"đã có sổ hồng"}'),
  ('project-tower-b', 'project', 'Hồ sơ dự án Tower B',
   'ho-so-du-an-tower-b.pdf', '2026-01-01', NULL, 'published',
   '1644fcd147b4ccdde9d044382f8a12805febcc55d2ae3175d985eabf326f43aa',
   '{"project_name":"Tower B","location":"Quận Hải Châu, Đà Nẵng","developer":"Công ty BĐS số 1","total_units":150,"handover_date":"2027-06-30","amenities":["hồ bơi","sân tennis"],"legal_status":"đã có sổ hồng"}')
ON CONFLICT (doc_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. campaigns — đợt giá (B7: giá/policy BẮT BUỘC gán campaign)
-- ---------------------------------------------------------------------------
INSERT INTO campaigns
  (campaign_key, project_key, effective_from, effective_to, source_doc_id, status)
VALUES
  ('tower-a-2026q2', 'tower-a', '2026-04-01', NULL, 'price-tower-a-2026q2', 'active'),
  ('tower-b-2026q1', 'tower-b', '2026-01-01', '2026-03-31', 'price-tower-b-2026q1', 'expired')
ON CONFLICT (campaign_key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. fact_subjects — 29 căn + 2 dự án (dedup theo subject_key)
-- ---------------------------------------------------------------------------
INSERT INTO fact_subjects (subject_key, subject_type, display_name, project_key, attrs)
SELECT t.subject_key, 'unit', t.display_name, t.project_key,
       jsonb_build_object('floor', t.floor)
FROM (VALUES
  -- Tower A (active) — 21 căn, giá 1.2 tỷ → 8 tỷ
  ('unit:tower-a/A10-01', 'Căn A10-01', 'tower-a', 10),
  ('unit:tower-a/A10-02', 'Căn A10-02', 'tower-a', 10),
  ('unit:tower-a/A10-03', 'Căn A10-03', 'tower-a', 10),
  ('unit:tower-a/A09-01', 'Căn A09-01', 'tower-a',  9),
  ('unit:tower-a/A09-02', 'Căn A09-02', 'tower-a',  9),
  ('unit:tower-a/A09-03', 'Căn A09-03', 'tower-a',  9),
  ('unit:tower-a/A08-01', 'Căn A08-01', 'tower-a',  8),
  ('unit:tower-a/A08-02', 'Căn A08-02', 'tower-a',  8),
  ('unit:tower-a/A08-03', 'Căn A08-03', 'tower-a',  8),
  ('unit:tower-a/A07-01', 'Căn A07-01', 'tower-a',  7),
  ('unit:tower-a/A07-02', 'Căn A07-02', 'tower-a',  7),
  ('unit:tower-a/A07-03', 'Căn A07-03', 'tower-a',  7),
  ('unit:tower-a/A06-01', 'Căn A06-01', 'tower-a',  6),
  ('unit:tower-a/A06-02', 'Căn A06-02', 'tower-a',  6),
  ('unit:tower-a/A05-01', 'Căn A05-01', 'tower-a',  5),
  ('unit:tower-a/A05-02', 'Căn A05-02', 'tower-a',  5),
  ('unit:tower-a/A05-03', 'Căn A05-03', 'tower-a',  5),
  ('unit:tower-a/A04-01', 'Căn A04-01', 'tower-a',  4),
  ('unit:tower-a/A04-02', 'Căn A04-02', 'tower-a',  4),
  ('unit:tower-a/A04-03', 'Căn A04-03', 'tower-a',  4),
  ('unit:tower-a/A03-01', 'Căn A03-01', 'tower-a',  3),
  -- Tower B (expired) — 8 căn, giá 5.3 tỷ → 9.5 tỷ
  ('unit:tower-b/B1-01', 'Căn B1-01', 'tower-b', 1),
  ('unit:tower-b/B1-02', 'Căn B1-02', 'tower-b', 1),
  ('unit:tower-b/B1-03', 'Căn B1-03', 'tower-b', 1),
  ('unit:tower-b/B2-01', 'Căn B2-01', 'tower-b', 2),
  ('unit:tower-b/B2-02', 'Căn B2-02', 'tower-b', 2),
  ('unit:tower-b/B2-03', 'Căn B2-03', 'tower-b', 2),
  ('unit:tower-b/B3-01', 'Căn B3-01', 'tower-b', 3),
  ('unit:tower-b/B3-02', 'Căn B3-02', 'tower-b', 3)
) AS t(subject_key, display_name, project_key, floor)
ON CONFLICT (subject_key) DO NOTHING;

INSERT INTO fact_subjects (subject_key, subject_type, display_name, project_key, attrs)
VALUES
  ('project:tower-a', 'project', 'Dự án Tower A', 'tower-a',
   '{"developer":"Công ty BĐS số 1","location":"Quận Liên Chiểu, Đà Nẵng","total_units":200}'),
  ('project:tower-b', 'project', 'Dự án Tower B', 'tower-b',
   '{"developer":"Công ty BĐS số 1","location":"Quận Hải Châu, Đà Nẵng","total_units":150}')
ON CONFLICT (subject_key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. facts — price_vnd + area_m2 cho 29 căn (NUMERIC(20,0) vnd / NUMERIC(10,2) m2)
--    Idempotent: NOT EXISTS guard theo (subject, fact_key, effective_from).
-- ---------------------------------------------------------------------------
INSERT INTO facts
  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,
   volatile, effective_from, effective_to, source_doc_id, extract_conf)
SELECT s.id, d.fact_key, NULL, d.campaign_key, d.value_num, d.unit, 'exact',
       FALSE, d.effective_from, d.effective_to, d.source_doc_id, 0.99
FROM (VALUES
  -- Tower A (active) — price_vnd / area_m2
  ('unit:tower-a/A10-01', 'price_vnd', 8000000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A10-01', 'area_m2',  85.50::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A10-02', 'price_vnd', 7600000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A10-02', 'area_m2',  82.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A10-03', 'price_vnd', 7200000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A10-03', 'area_m2',  78.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A09-01', 'price_vnd', 6800000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A09-01', 'area_m2',  80.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A09-02', 'price_vnd', 6400000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A09-02', 'area_m2',  75.50::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A09-03', 'price_vnd', 6000000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A09-03', 'area_m2',  72.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A08-01', 'price_vnd', 5500000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A08-01', 'area_m2',  70.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A08-02', 'price_vnd', 5200000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A08-02', 'area_m2',  68.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A08-03', 'price_vnd', 4900000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A08-03', 'area_m2',  65.50::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A07-01', 'price_vnd', 4500000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A07-01', 'area_m2',  64.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A07-02', 'price_vnd', 4200000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A07-02', 'area_m2',  62.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A07-03', 'price_vnd', 3900000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A07-03', 'area_m2',  60.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A06-01', 'price_vnd', 3500000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A06-01', 'area_m2',  58.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A06-02', 'price_vnd', 3200000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A06-02', 'area_m2',  56.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A05-01', 'price_vnd', 2700000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A05-01', 'area_m2',  54.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A05-02', 'price_vnd', 2400000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A05-02', 'area_m2',  52.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A05-03', 'price_vnd', 2000000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A05-03', 'area_m2',  50.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A04-01', 'price_vnd', 1800000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A04-01', 'area_m2',  48.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A04-02', 'price_vnd', 1600000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A04-02', 'area_m2',  46.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A04-03', 'price_vnd', 1400000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A04-03', 'area_m2',  44.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A03-01', 'price_vnd', 1200000000::NUMERIC(20,0), 'vnd', 'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  ('unit:tower-a/A03-01', 'area_m2',  42.00::NUMERIC(10,2), 'm2',  'tower-a-2026q2', 'price-tower-a-2026q2', '2026-04-01', NULL),
  -- Tower B (expired) — interval đã đóng [2026-01-01, 2026-03-31)
  ('unit:tower-b/B1-01', 'price_vnd', 9500000000::NUMERIC(20,0), 'vnd', 'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B1-01', 'area_m2',  95.00::NUMERIC(10,2), 'm2',  'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B1-02', 'price_vnd', 9000000000::NUMERIC(20,0), 'vnd', 'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B1-02', 'area_m2',  92.00::NUMERIC(10,2), 'm2',  'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B1-03', 'price_vnd', 8500000000::NUMERIC(20,0), 'vnd', 'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B1-03', 'area_m2',  88.00::NUMERIC(10,2), 'm2',  'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B2-01', 'price_vnd', 7800000000::NUMERIC(20,0), 'vnd', 'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B2-01', 'area_m2',  84.00::NUMERIC(10,2), 'm2',  'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B2-02', 'price_vnd', 7200000000::NUMERIC(20,0), 'vnd', 'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B2-02', 'area_m2',  80.00::NUMERIC(10,2), 'm2',  'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B2-03', 'price_vnd', 6600000000::NUMERIC(20,0), 'vnd', 'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B2-03', 'area_m2',  76.00::NUMERIC(10,2), 'm2',  'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B3-01', 'price_vnd', 5900000000::NUMERIC(20,0), 'vnd', 'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B3-01', 'area_m2',  72.00::NUMERIC(10,2), 'm2',  'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B3-02', 'price_vnd', 5300000000::NUMERIC(20,0), 'vnd', 'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31'),
  ('unit:tower-b/B3-02', 'area_m2',  68.00::NUMERIC(10,2), 'm2',  'tower-b-2026q1', 'price-tower-b-2026q1', '2026-01-01', '2026-03-31')
) AS d(subject_key, fact_key, value_num, unit, campaign_key, source_doc_id, effective_from, effective_to)
JOIN fact_subjects s ON s.subject_key = d.subject_key
WHERE NOT EXISTS (
  SELECT 1 FROM facts f
  WHERE f.subject_id = s.id AND f.fact_key = d.fact_key
    AND f.policy_key IS NULL AND f.effective_from = d.effective_from
);

-- ---------------------------------------------------------------------------
-- 5. facts — chính sách vay (policy facts) — 2 policy ngân hàng mỗi căn
--    bank_a: deposit 25.00 / term 180 / interest 8.5000
--    bank_b: deposit 30.00 / term 240 / interest 8.0000
--    A06-01: support  0.00 / term 120 / interest 0.0000  (0% trả trước — ÁP DỤNG thay vì ngân hàng)
--    A04-03: KHÔNG policy → không xuất hiện trong v_unit_offers (edge case §3.4)
--    CROSS JOIN unit × policy template; NOT EXISTS guard theo (subject, policy, fact_key, from).
-- ---------------------------------------------------------------------------
INSERT INTO facts
  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,
   volatile, effective_from, effective_to, source_doc_id, extract_conf)
SELECT s.id, p.fact_key, p.policy_key, p.campaign_key, p.value_num, p.unit, 'exact',
       FALSE, p.effective_from, p.effective_to, p.source_doc_id, 0.95
FROM (VALUES
  -- Template policy theo từng căn (policy_key, fact_key, value_num, unit)
  -- Tower A active — bank_a / bank_b cho mọi căn trừ A06-01 (support) và A04-03 (không policy)
  ('unit:tower-a/A10-01', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A10-01', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A10-01', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A10-01', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A10-01', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A10-01', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A10-02', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A10-02', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A10-02', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A10-02', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A10-02', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A10-02', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A10-03', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A10-03', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A10-03', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A10-03', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A10-03', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A10-03', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A09-01', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A09-01', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A09-01', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A09-01', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A09-01', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A09-01', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A09-02', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A09-02', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A09-02', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A09-02', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A09-02', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A09-02', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A09-03', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A09-03', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A09-03', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A09-03', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A09-03', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A09-03', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A08-01', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A08-01', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A08-01', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A08-01', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A08-01', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A08-01', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A08-02', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A08-02', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A08-02', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A08-02', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A08-02', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A08-02', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A08-03', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A08-03', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A08-03', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A08-03', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A08-03', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A08-03', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A07-01', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A07-01', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A07-01', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A07-01', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A07-01', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A07-01', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A07-02', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A07-02', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A07-02', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A07-02', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A07-02', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A07-02', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A07-03', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A07-03', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A07-03', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A07-03', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A07-03', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A07-03', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A06-01', 'support', 'deposit_pct',       0.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A06-01', 'support', 'term_months',       120::NUMERIC, 'months'),
  ('unit:tower-a/A06-01', 'support', 'interest_rate_pct', 0.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A06-02', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A06-02', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A06-02', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A06-02', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A06-02', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A06-02', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A05-01', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A05-01', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A05-01', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A05-01', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A05-01', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A05-01', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A05-02', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A05-02', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A05-02', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A05-02', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A05-02', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A05-02', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A05-03', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A05-03', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A05-03', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A05-03', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A05-03', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A05-03', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A04-01', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A04-01', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A04-01', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A04-01', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A04-01', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A04-01', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A04-02', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A04-02', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A04-02', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A04-02', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A04-02', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A04-02', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A03-01', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A03-01', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-a/A03-01', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-a/A03-01', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-a/A03-01', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-a/A03-01', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  -- Tower B (expired) — policy cùng interval đã đóng
  ('unit:tower-b/B1-01', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B1-01', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-b/B1-01', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B1-01', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B1-01', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-b/B1-01', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B1-02', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B1-02', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-b/B1-02', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B1-02', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B1-02', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-b/B1-02', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B1-03', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B1-03', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-b/B1-03', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B1-03', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B1-03', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-b/B1-03', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B2-01', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B2-01', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-b/B2-01', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B2-01', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B2-01', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-b/B2-01', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B2-02', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B2-02', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-b/B2-02', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B2-02', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B2-02', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-b/B2-02', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B2-03', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B2-03', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-b/B2-03', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B2-03', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B2-03', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-b/B2-03', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B3-01', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B3-01', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-b/B3-01', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B3-01', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B3-01', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-b/B3-01', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B3-02', 'bank_a', 'deposit_pct',        25.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B3-02', 'bank_a', 'term_months',        180::NUMERIC, 'months'),
  ('unit:tower-b/B3-02', 'bank_a', 'interest_rate_pct',  8.5000::NUMERIC(6,4), 'pct'),
  ('unit:tower-b/B3-02', 'bank_b', 'deposit_pct',        30.00::NUMERIC(5,2), 'pct'),
  ('unit:tower-b/B3-02', 'bank_b', 'term_months',        240::NUMERIC, 'months'),
  ('unit:tower-b/B3-02', 'bank_b', 'interest_rate_pct',  8.0000::NUMERIC(6,4), 'pct')
) AS p(subject_key, policy_key, fact_key, value_num, unit)
JOIN fact_subjects s ON s.subject_key = p.subject_key
CROSS JOIN LATERAL (
  SELECT d2.campaign_key, d2.effective_from, d2.effective_to, d2.source_doc_id
  FROM documents d2
  WHERE d2.doc_id = CASE WHEN p.subject_key LIKE 'unit:tower-b/%'
                         THEN 'price-tower-b-2026q1' ELSE 'price-tower-a-2026q2' END
) AS d
WHERE NOT EXISTS (
  SELECT 1 FROM facts f
  WHERE f.subject_id = s.id AND f.fact_key = p.fact_key
    AND COALESCE(f.policy_key, '') = p.policy_key
    AND f.effective_from = d.effective_from
);

COMMIT;
