-- rag-real-estate - Seed project_config registry (story 8.2 / ISSUE-01, idempotent, UTF-8).
-- Run after db/schema.sql (or after the 2026-08-21 migration on an existing DB).
--
-- Story 10.3: `location` carries the detailed Vietnamese address the FE project
-- picker shows (corrected ward for Camellia — Thọ Quang), and `is_hot` flags the
-- HOT project (Camellia=true) so the picker leads with it (GET /api/projects).
--
-- One row per project key. media is the R2 video/media contract the greeting
-- widget consumes; the JSONB entries carry R2 OBJECT KEYS (media/video/... and
-- images/matbang/...) plus display metadata, and api/application/services/
-- media_config.py resolves them against settings.r2_public_base when building
-- the public URL list. Storing keys (not absolute URLs) keeps the seed
-- environment-portable: the same row works for dev and prod R2 hosts.
--
-- Camellia keeps its three processed clips (see ingest/upload_videos_r2.py);
-- Soleil has no processed video files yet, so its media is '[]' (honest — no
-- fabricated clips).
--
-- Reserved keys (D5): '_legacy' = untagged legacy documents awaiting review;
-- '_training' = training namespace (story 8.6). Both are namespaces, not real
-- projects, so they are NOT inserted here — but the leading-underscore rule is
-- the hard guard: a real project key must never start with '_'. Keep it that way.

BEGIN;

INSERT INTO project_config
  (project_key, ten_phap_ly, ten_thuong_mai, vi_tri,
   location, is_hot,
   geo_center_lat, geo_center_lng, hotline, media,
   sales_kit_file, persona_file, status, publish_at)
VALUES
  -- The Camellia: geo center verified against OSM street geometry + Google Maps
  -- project pin (16.1056072/108.2563337) — see db/seed/static_places.json note.
  -- Story 10.3: the ward is Thọ Quang (quận Sơn Trà) per the legal docs — the
  -- old vi_tri ("phường Sơn Trà") was the district name used as a ward; the
  -- `location` column carries the corrected detailed address. HOT project.
  ('camellia',
   'Trung tâm Thương mại, văn phòng cho thuê và nhà ở cao tầng',
   'The Camellia Son Tra - Da Nang',
   'Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Sơn Trà, Đà Nẵng',
   'Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Thọ Quang, quận Sơn Trà, Đà Nẵng',
   true,
   16.1052, 108.2558,
   '0345 747 138',
   jsonb_build_array(
     jsonb_build_object('title', 'The Camellia - Brand Film (Web)', 'kind', 'brand',
                        'object_key', 'media/video/brand-film-web.mp4',
                        'poster_key', 'images/matbang/matbang-02.png',
                        'width', 1920, 'height', 1080, 'duration', NULL, 'bytes_mb', NULL),
     jsonb_build_object('title', 'The Camellia - Brand Film (Original)', 'kind', 'brand',
                        'object_key', 'media/video/brand-film-faststart.mp4',
                        'poster_key', 'images/matbang/matbang-02.png',
                        'width', 1920, 'height', 1080, 'duration', NULL, 'bytes_mb', NULL),
     jsonb_build_object('title', 'The Camellia - Drone Overview (DJI)', 'kind', 'drone',
                        'object_key', 'media/video/dji-orbit-faststart.mp4',
                        'poster_key', 'images/matbang/matbang-02.png',
                        'width', NULL, 'height', NULL, 'duration', NULL, 'bytes_mb', NULL)
   ),
   NULL, NULL, 'active', NULL),

  -- The Soleil: geo center = Wyndham Soleil Danang OSM pin (16.0710756/108.2436243,
  -- An Hải, Sơn Trà), the project sales office at 194 Võ Nguyên Giáp. No processed
  -- video files exist for Soleil yet -> media stays empty. Not HOT.
  ('soleil',
   'Tổ hợp Ánh Dương - Soleil (tên dự án đầu tư: PHẦN HẦM, KHỐI D VÀ KHỐI A1 TỔ HỢP ÁNH DƯƠNG - SOLEIL)',
   'The Soleil Đà Nẵng (Bộ sưu tập căn hộ khách sạn hạng thương gia - C Suite Collection)',
   'Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng',
   'Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng',
   false,
   16.0710756, 108.2436243,
   NULL,
   '[]'::jsonb,
   NULL, NULL, 'active', NULL)
ON CONFLICT (project_key) DO UPDATE SET
  ten_phap_ly     = EXCLUDED.ten_phap_ly,
  ten_thuong_mai  = EXCLUDED.ten_thuong_mai,
  vi_tri          = EXCLUDED.vi_tri,
  location        = EXCLUDED.location,
  is_hot          = EXCLUDED.is_hot,
  geo_center_lat  = EXCLUDED.geo_center_lat,
  geo_center_lng  = EXCLUDED.geo_center_lng,
  hotline         = EXCLUDED.hotline,
  media           = EXCLUDED.media,
  sales_kit_file  = EXCLUDED.sales_kit_file,
  persona_file    = EXCLUDED.persona_file,
  status          = EXCLUDED.status,
  publish_at      = EXCLUDED.publish_at,
  updated_at      = now();

COMMIT;
