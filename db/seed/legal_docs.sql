-- rag-real-estate — Seed: base legal documents (T4) into registry `documents`.
-- MOCK/DEMO DATA: placeholder hashes + effective dates, awaiting real data (plan §10); not for production.
-- 10 kind='legal' docs; content_hash = placeholder sha256('seed:<doc_id>'), ingest replaces it with the real file hash (AD-7).
-- Superseded laws (Land Law 2013, Decree 43/2014, Decree 99/2022) are marked expired so the validity filter
-- (plan §4.3) + RLS docs_pub_select (SELECT only status='published') drop them from retrieval.
-- Idempotent | UTF-8 | Plan §3.3 + §10 Days 3-4

BEGIN;

INSERT INTO documents
  (doc_id, kind, title, source_file, effective_from, effective_to, status, content_hash, metadata)
VALUES
  -- Land Law 2024 — effective 01/01/2025 (replaces Land Law 2013)
  ('ldd-2024', 'legal', 'Luật Đất đai 2024 (31/2024/QH15)',
   'luat-dat-dai-2024.pdf', '2025-01-01', NULL, 'published',
   '78be2e625e4dc939ad75372ecbb5ac99c879890dcbb09edc51993e0344079c81',
   '{"document_number":"31/2024/QH15","document_type":"luat","issuer":"Quốc hội","issue_date":"2024-01-18","related_docs":["ldd-2013","nd-43-2014"],"keywords":["chuyển nhượng","thế chấp","cầm cố","quy hoạch","sổ đỏ","Điều 27","Điều 45","Điều 191"]}'),

  -- Real Estate Business Law 2023 — effective 01/08/2024
  ('lkdbds-2023', 'legal', 'Luật Kinh doanh bất động sản 2023 (29/2023/QH15)',
   'luat-kinh-doanh-bds-2023.pdf', '2024-08-01', NULL, 'published',
   'ac2e4aeb30a17f4cd6cdeddcc00dc8ff5314042f270015809cc8bb312f19a702',
   '{"document_number":"29/2023/QH15","document_type":"luat","issuer":"Quốc hội","issue_date":"2023-11-28","keywords":["môi giới","chuyển nhượng dự án","nhà ở hình thành trong tương lai","Điều 24","Điều 40","Điều 61"]}'),

  -- Civil Code 2015 — effective 01/01/2017 (Arts. 309-321 mortgage; Art. 328 deposit)
  ('blds-2015', 'legal', 'Bộ luật Dân sự 2015 (91/2015/QH13) — Điều 309-321, 328',
   'bo-luat-dan-su-2015.pdf', '2017-01-01', NULL, 'published',
   'f2b2957c504c16663d535da17a900440c3c451a7082e5fb1753bbd147ebddaa0',
   '{"document_number":"91/2015/QH13","document_type":"luat","issuer":"Quốc hội","issue_date":"2015-11-24","keywords":["thế chấp","cầm cố","đặt cọc","vô hiệu","Điều 123","Điều 309","Điều 317","Điều 320","Điều 321","Điều 328"]}'),

  -- Housing Law 2023 — effective 01/08/2024
  ('lno-2023', 'legal', 'Luật Nhà ở 2023 (27/2023/QH15)',
   'luat-nha-o-2023.pdf', '2024-08-01', NULL, 'published',
   '89a0fd8a18e779bce2ad6df7afe15253363df8a8114333b9566cbc2c802d77e6',
   '{"document_number":"27/2023/QH15","document_type":"luat","issuer":"Quốc hội","issue_date":"2023-11-27","keywords":["nhà chung cư","nhà ở","quyền sở hữu","bảo trì"]}'),

  -- Decree 101/2024 — security-measure registration / ownership-transfer procedure (effective date needs verification)
  ('nd-101-2024', 'legal', 'Nghị định 101/2024/NĐ-CP về đăng ký biện pháp bảo đảm',
   'nd-101-2024.pdf', '2025-01-01', NULL, 'published',
   '81f3b0c8cffdb6ec3c5bad9c7a725f9f4326bafc1068cfec2bfe27f3c1f83892',
   '{"document_number":"101/2024/NĐ-CP","document_type":"nghi-dinh","issuer":"Chính phủ","issue_date":"2024-07-29","keywords":["đăng ký biện pháp bảo đảm","xóa thế chấp","giải chấp","sang tên"]}'),

  -- Decree 96/2024 — implementing rules for Housing Law 2023 (effective date needs verification)
  ('nd-96-2024', 'legal', 'Nghị định 96/2024/NĐ-CP quy định chi tiết một số điều của Luật Nhà ở',
   'nd-96-2024.pdf', '2024-08-01', NULL, 'published',
   '7fb9d885f5293343bfeea9abe39b27dcb4bb3e46de31a4c2f0e95762c5e004f8',
   '{"document_number":"96/2024/NĐ-CP","document_type":"nghi-dinh","issuer":"Chính phủ","issue_date":"2024-07-24","keywords":["nhà ở xã hội","nhà chung cư","quản lý sử dụng"]}'),

  -- Decree 99/2022 — security-measure registration (superseded by Decree 101/2024)
  ('nd-99-2022', 'legal', 'Nghị định 99/2022/NĐ-CP về đăng ký biện pháp bảo đảm (cũ)',
   'nd-99-2022.pdf', '2022-08-15', '2024-12-31', 'expired',
   '51b971aaaa4334e355f9a8403e9fd31a9dd230c8a18569d905be61c7e0ab926d',
   '{"document_number":"99/2022/NĐ-CP","document_type":"nghi-dinh","issuer":"Chính phủ","issue_date":"2022-08-15","keywords":["đăng ký biện pháp bảo đảm","giải chấp","xóa đăng ký"]}'),

  -- Circular 04/2024/TT-BXD — condominium management (effective date needs verification)
  ('tt-04-2024-bxd', 'legal', 'Thông tư 04/2024/TT-BXD về quản lý sử dụng nhà chung cư',
   'tt-04-2024-bxd.pdf', '2024-08-15', NULL, 'published',
   'cefc61152c3d0a55dc4e21660b1f4ecff950e06a242bbbcbf6b693c67b486ee5',
   '{"document_number":"04/2024/TT-BXD","document_type":"thong-tu","issuer":"Bộ Xây dựng","issue_date":"2024-06-11","keywords":["nhà chung cư","phí bảo trì","quản lý vận hành"]}'),

  -- Decree 43/2014 — implementing rules for Land Law 2013 (superseded)
  ('nd-43-2014', 'legal', 'Nghị định 43/2014/NĐ-CP hướng dẫn Luật Đất đai 2013 (cũ)',
   'nd-43-2014.pdf', '2014-07-01', '2024-12-31', 'expired',
   '5c36ad0868e7893dc2ec48a65bf53c6fb5bdfd9c9b3d30baaa9cebd2b62dc86c',
   '{"document_number":"43/2014/NĐ-CP","document_type":"nghi-dinh","issuer":"Chính phủ","issue_date":"2014-05-15","keywords":["giao đất","cho thuê đất","chuyển nhượng"]}'),

  -- Land Law 2013 — expired 31/12/2024 (replaced by 31/2024/QH15)
  ('ldd-2013', 'legal', 'Luật Đất đai 2013 (45/2013/QH13) — hết hiệu lực',
   'luat-dat-dai-2013.pdf', '2014-07-01', '2024-12-31', 'expired',
   '0f7af0f649c7a01c5745d7f3b0a5d80ff1b5f677734e6a2922480fac41d932fc',
   '{"document_number":"45/2013/QH13","document_type":"luat","issuer":"Quốc hội","issue_date":"2013-06-29","keywords":["chuyển nhượng","thế chấp","Điều 188","thời hạn sử dụng đất"]}')
ON CONFLICT (doc_id) DO NOTHING;

COMMIT;
