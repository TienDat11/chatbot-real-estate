-- lead/sales schema (Epic 5/6 Story 6.4)
-- Run AFTER db/schema.sql. Requires PostgreSQL 16.6+.

-- 1. sales table — môi giới/sales
CREATE TABLE IF NOT EXISTS sales (
  id            BIGSERIAL PRIMARY KEY,
  access_key    TEXT        NOT NULL UNIQUE DEFAULT replace(gen_random_uuid()::text,'-',''),
  full_name     TEXT        NOT NULL,
  role          TEXT,
  phone         TEXT,
  is_active     BOOLEAN     NOT NULL DEFAULT true,
  priority      INT         NOT NULL DEFAULT 0,
  last_seen_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sales_full_name ON sales (full_name);
CREATE INDEX IF NOT EXISTS idx_sales_active ON sales (is_active) WHERE is_active;

-- 2. leads table — khách để lại SĐT
CREATE TABLE IF NOT EXISTS leads (
  id                    BIGSERIAL PRIMARY KEY,
  session_id            TEXT,
  project_key           TEXT,                 -- project registry key (story 10.1, G1: required at submit)
  device_id             TEXT,                 -- anonymous persistent device (D7); PII once paired with phone
  name                  TEXT,
  phone                 TEXT        NOT NULL,
  consent               BOOLEAN     NOT NULL DEFAULT false,
  note                  TEXT,
  budget_vnd            NUMERIC(20,0),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  status                TEXT        NOT NULL DEFAULT 'new'
    CHECK (status IN ('new','assigned','called','callback','no_answer','booked','lost','expired')),
  assigned_sales_id     BIGINT REFERENCES sales(id),
  lock_expires_at       TIMESTAMPTZ,
  escal_count           INT         NOT NULL DEFAULT 0,
  last_action_at        TIMESTAMPTZ,
  closed_at             TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_leads_active ON leads (assigned_sales_id, status, lock_expires_at)
  WHERE status = 'assigned';
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads (phone);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status);
-- Re-approach lookups: "khách này đã từ chối dự án nào" per device (story 9.4)
-- and per-project lead filtering for the CRM (story 9.3).
CREATE INDEX IF NOT EXISTS idx_leads_project ON leads (project_key);
CREATE INDEX IF NOT EXISTS idx_leads_device ON leads (device_id);

-- 3. sales_assignment_log — nhật ký gán/phát hành lead
CREATE TABLE IF NOT EXISTS sales_assignment_log (
  id          BIGSERIAL PRIMARY KEY,
  lead_id     BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  sales_id    BIGINT REFERENCES sales(id) ON DELETE CASCADE,
  action      TEXT   NOT NULL
    CHECK (action IN ('assign','release','escalate','call','callback','no_answer','booked','lost','expired')),
  note        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sal_log_lead ON sales_assignment_log (lead_id);
CREATE INDEX IF NOT EXISTS idx_sal_sales ON sales_assignment_log (sales_id);

-- 4. Seed 5 sales from data/_processed/sales_contacts.json
-- Field "name" -> full_name. Role mapping from role field.
INSERT INTO sales (full_name, role, phone, is_active, priority)
SELECT v.full_name, v.role, v.phone, v.is_active, v.priority
FROM (VALUES
  ('Nguyễn Thị Nguyệt', 'Giám đốc Kinh doanh', '0345747138', true, 10),
  ('Nguyễn Quyền Anh', 'Trưởng phòng Kinh doanh', '0939963769', true, 9),
  ('Đào Duy Dự', 'Trưởng phòng Kinh doanh', '0826768386', true, 8),
  ('Trịnh Thị Quý', 'Chuyên viên kinh doanh', '0344351069', true, 5),
  ('Trần Đình Huy', 'Chuyên viên kinh doanh', '0372572984', true, 5)
) AS v(full_name, role, phone, is_active, priority)
ON CONFLICT (full_name) DO NOTHING;

-- 5. Function to get last_assigned_at per sales (for LRU routing)
CREATE OR REPLACE FUNCTION get_sales_last_assigned(p_sales_id BIGINT)
RETURNS TIMESTAMPTZ
LANGUAGE sql
STABLE
AS $$
  SELECT MAX(created_at) FROM sales_assignment_log
  WHERE sales_id = p_sales_id AND action IN ('assign','escalate');
$$;

-- 6. Function: pick next sales LRU-first (NULLS FIRST), tiebreak priority DESC
CREATE OR REPLACE FUNCTION pick_next_sales(p_exclude_ids BIGINT[] DEFAULT '{}')
RETURNS SETOF sales
LANGUAGE sql
STABLE
AS $$
  SELECT s.*
  FROM sales s
  WHERE s.is_active
    AND s.id != ALL(p_exclude_ids)
  ORDER BY get_sales_last_assigned(s.id) ASC NULLS FIRST, s.priority DESC
  LIMIT 1;
$$;
