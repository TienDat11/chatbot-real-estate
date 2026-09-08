-- rag-real-estate — Seed Soleil nightly rental reference facts (idempotent, UTF-8).
-- Run after db/schema.sql + db/camellia_estimate.sql (adds facts.trust_level).
--
-- Additive to db/seed/soleil_campaign.sql: the campaign seed owns the SALE
-- price/policy facts; this file owns the nightly RENTAL reference rates from
-- data/_processed/soleil/rental_projections.json (PA tự vận hành + PA ủy thác).
-- Rows carry source_chunk_id NULL, so ingest/run_soleil_ingest.py preserves
-- them through load_document(preserve_seed_facts=True) for rental-soleil-2026q3.
--
-- Retrieval-facing text (value_text) uses the natural query phrases a customer
-- types ("giá thuê 1 đêm", "qua đêm", "studio", "2,8 triệu đồng/đêm",
-- "view biển", "Võ Nguyên Giáp") so the confirmed nightly figure wins the
-- ranking battle over the "chưa công bố" reading of the ủy thác QnA chunk.
--
-- Idempotent: every INSERT is guarded by WHERE NOT EXISTS on the exact
-- (subject, fact_key, policy_key, effective_from) tuple — re-running is a no-op.

BEGIN;

INSERT INTO facts
  (subject_id, fact_key, policy_key, campaign_key, value_num, value_text, unit,
   quality, volatile, effective_from, effective_to, source_doc_id, extract_conf,
   trust_level)
SELECT s.id, 'rental_price_vnd_ngay', NULL, 'soleil-2026q3',
       d.value_num::NUMERIC(20,0),
       'Giá thuê 1 đêm (qua đêm) căn Studio tại The Soleil Đà Nẵng (view biển, '
       'mặt đường Võ Nguyên Giáp): khoảng 2,8 triệu đồng/đêm (2.800.000 VND/đêm)',
       'vnd', 'approx', FALSE, '2026-08-21'::date, NULL,
       'rental-soleil-2026q3', 0.80, 'estimate'
FROM fact_subjects s
JOIN (VALUES
  ('unit:soleil/a1-stu', 2800000),
  ('unit:soleil/d-stu',  2800000)
) AS d(subject_key, value_num) ON s.subject_key = d.subject_key
WHERE NOT EXISTS (
  SELECT 1 FROM facts f
  WHERE f.subject_id = s.id AND f.fact_key = 'rental_price_vnd_ngay'
    AND COALESCE(f.policy_key, '') = ''
    AND f.effective_from = '2026-08-21'::date
);

INSERT INTO facts
  (subject_id, fact_key, policy_key, campaign_key, value_num, value_text, unit,
   quality, volatile, effective_from, effective_to, source_doc_id, extract_conf,
   trust_level)
SELECT s.id, 'rental_price_vnd_ngay', NULL, 'soleil-2026q3',
       d.value_num::NUMERIC(20,0),
       d.value_text, 'vnd', 'approx', FALSE, '2026-08-21'::date, NULL,
       'rental-soleil-2026q3', 0.80, 'estimate'
FROM fact_subjects s
JOIN (VALUES
  ('unit:soleil/d-stu',  2800000,
   'Giá thuê 1 đêm (qua đêm) căn Studio tại The Soleil Đà Nẵng (view biển, mặt '
   'đường Võ Nguyên Giáp): khoảng 2,8 triệu đồng/đêm (2.800.000 VND/đêm)'),
  ('unit:soleil/d-1br',  4500000,
   'Giá thuê 1 đêm (qua đêm) căn 1BR tại The Soleil Đà Nẵng (view biển, mặt '
   'đường Võ Nguyên Giáp): khoảng 4,5 triệu đồng/đêm (4.500.000 VND/đêm)'),
  ('unit:soleil/d-2br',  9000000,
   'Giá thuê 1 đêm (qua đêm) căn 2BR tại The Soleil Đà Nẵng (view biển, mặt '
   'đường Võ Nguyên Giáp): khoảng 9 triệu đồng/đêm (9.000.000 VND/đêm)')
) AS d(subject_key, value_num, value_text) ON s.subject_key = d.subject_key
WHERE NOT EXISTS (
  SELECT 1 FROM facts f
  WHERE f.subject_id = s.id AND f.fact_key = 'rental_price_vnd_ngay'
    AND COALESCE(f.policy_key, '') = ''
    AND f.effective_from = '2026-08-21'::date
);

-- Project-level summary text fact (one row, all natural query keywords).
INSERT INTO facts
  (subject_id, fact_key, policy_key, campaign_key, value_num, value_text, unit,
   quality, volatile, effective_from, effective_to, source_doc_id, extract_conf,
   trust_level)
SELECT s.id, 'rental_note', NULL, 'soleil-2026q3', NULL,
       'Giá thuê 1 đêm (qua đêm) tại The Soleil Đà Nẵng (view biển, mặt đường '
       'Võ Nguyên Giáp): Studio khoảng 2,8 triệu đồng/đêm (2.800.000 VND/đêm), '
       '1BR khoảng 4,5 triệu đồng/đêm, 2BR khoảng 9 triệu đồng/đêm — theo bảng '
       'tính dòng tiền thuê ước tính, không phải cam kết lợi nhuận cho thuê.',
       'enum', 'approx', FALSE, '2026-08-21'::date, NULL,
       'rental-soleil-2026q3', 0.80, 'estimate'
FROM fact_subjects s
WHERE s.subject_key = 'project:soleil'
AND NOT EXISTS (
  SELECT 1 FROM facts f
  WHERE f.subject_id = s.id AND f.fact_key = 'rental_note'
    AND COALESCE(f.policy_key, '') = ''
    AND f.effective_from = '2026-08-21'::date
);

COMMIT;
