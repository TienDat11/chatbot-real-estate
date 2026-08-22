-- rag-real-estate — Migration 2026-08-23: percent-encode raw '%' in
-- images.url_cdn (QA defect D2).
--
-- The image ingest (ingest/images_ingest.py) used to paste the R2 object key
-- into the URL verbatim. Object keys are S3-legal but not URL-safe: the
-- Camellia row thanh-toan-som-95 carries a literal '%' ('...som_95%.jpg'),
-- which browsers refuse to load (ERR_BLOCKED_BY_ORB; raw '%' is HTTP 400).
-- The builder now quotes the key at build time (build_cdn_url), and this
-- migration repairs the rows already stored unencoded.
--
-- Idempotent: the WHERE matches only '%' NOT followed by two hex digits
-- (a valid escape); after the rewrite every '%' is part of '%25', so a
-- re-run is a no-op. Already-correct escapes (%20 etc.) are never touched.
--
-- [LR-23/08] Down-note (rollback):
--   BEGIN;
--   UPDATE images SET url_cdn = replace(url_cdn, '%25', '%')
--   WHERE image_id = 'thanh-toan-som-95';
--   COMMIT;
-- Run a backup BEFORE this migration per the Epic 7 runbook.

BEGIN;

UPDATE images
SET url_cdn = regexp_replace(url_cdn, '%(?![0-9A-Fa-f]{2})', '%25', 'g'),
    updated_at = now()
WHERE url_cdn ~ '%(?![0-9A-Fa-f]{2})';

COMMIT;
