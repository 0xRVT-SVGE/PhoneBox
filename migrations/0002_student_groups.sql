-- ============================================================
-- MIGRATION 0002 — Student groups & box access refactor
-- Run AFTER 0001_multi_box.sql
-- Safe to run multiple times (idempotent via IF EXISTS / IF NOT EXISTS)
-- ============================================================

-- ── Drop replaced columns ─────────────────────────────────────────────────

-- year_group SMALLINT → replaced by year_code TEXT + sub_group_codes TEXT[]
ALTER TABLE students
    DROP COLUMN IF EXISTS year_group;

-- accept_year_groups INT[] → replaced by accept_group_codes TEXT[]
ALTER TABLE boxes
    DROP COLUMN IF EXISTS accept_year_groups;

-- allowed_box_slugs TEXT[] → removed; access is now determined by
-- student group ↔ box accept_group_codes, not per-phone slug lists
ALTER TABLE phones
    DROP COLUMN IF EXISTS allowed_box_slugs;

-- ── Widen phone_size CHECK to include extra_large ─────────────────────────
-- 0001 created: CHECK (phone_size IN ('standard', 'large'))
-- Config now includes 'extra_large'; drop and recreate the constraint.
ALTER TABLE phones DROP CONSTRAINT IF EXISTS phones_phone_size_check;
ALTER TABLE phones
    ADD CONSTRAINT phones_phone_size_check
    CHECK (phone_size IN ('standard', 'large', 'extra_large'));


-- ── Student group columns ─────────────────────────────────────────────────

ALTER TABLE students
    ADD COLUMN IF NOT EXISTS year_code       TEXT   DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS sub_group_codes TEXT[] DEFAULT NULL;

COMMENT ON COLUMN students.year_code IS
    'Primary year group code (e.g. ''year1'', ''year2''). One value only. NULL = unrestricted/staff.';

COMMENT ON COLUMN students.sub_group_codes IS
    'Optional subcategory codes within the year (e.g. ''{year1.1, boys}''). '
    'NULL or empty = access to ALL subcategories of year_code.';

CREATE INDEX IF NOT EXISTS idx_students_year_code
    ON students(year_code);

CREATE INDEX IF NOT EXISTS idx_students_sub_groups
    ON students USING GIN(sub_group_codes);


-- ── Box access columns ────────────────────────────────────────────────────

ALTER TABLE boxes
    ADD COLUMN IF NOT EXISTS accept_group_codes   TEXT[] DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS accepted_phone_sizes TEXT[] DEFAULT NULL;

COMMENT ON COLUMN boxes.accept_group_codes IS
    'Group codes allowed in this box. NULL = any student. '
    'Intersection with student effective groups must be non-empty for access.';

COMMENT ON COLUMN boxes.accepted_phone_sizes IS
    'Phone sizes accepted in this box. NULL = any size. '
    'e.g. ''{standard}'', ''{large}'', ''{standard, large}''.';

CREATE INDEX IF NOT EXISTS idx_boxes_accept_groups
    ON boxes USING GIN(accept_group_codes);

CREATE INDEX IF NOT EXISTS idx_boxes_accept_sizes
    ON boxes USING GIN(accepted_phone_sizes);


-- ── Update box seed rows (adjust slugs to your deployment) ───────────────
-- Year boxes: accept year students, standard phones only
UPDATE boxes SET
    accept_group_codes   = '{year1}',
    accepted_phone_sizes = '{standard}'
WHERE box_slug = 'year_1';

UPDATE boxes SET
    accept_group_codes   = '{year2}',
    accepted_phone_sizes = '{standard}'
WHERE box_slug = 'year_2';

UPDATE boxes SET
    accept_group_codes   = '{year3}',
    accepted_phone_sizes = '{standard}'
WHERE box_slug = 'year_3';

-- Large phone box: any student, large phones only
UPDATE boxes SET
    accept_group_codes   = NULL,
    accepted_phone_sizes = '{large}'
WHERE box_slug = 'large';

-- Shared box: any student, any phone size
UPDATE boxes SET
    accept_group_codes   = NULL,
    accepted_phone_sizes = NULL
WHERE box_slug = 'shared';


-- ── Refresh v_box_dashboard view (if it exists) ──────────────────────────
-- Drop and recreate to pick up new column names
DROP VIEW IF EXISTS v_box_dashboard;

CREATE OR REPLACE VIEW v_box_dashboard AS
SELECT
    b.box_id,
    b.box_slug,
    b.box_name,
    b.accept_group_codes,
    b.accepted_phone_sizes,
    b.max_slots,
    COUNT(ps.id) FILTER (WHERE ps.retrieved_at IS NULL) AS phones_stored
FROM boxes b
LEFT JOIN locations l  ON l.box_id = b.box_id
LEFT JOIN phone_storage ps ON ps.lid = l.lid
GROUP BY b.box_id, b.box_slug, b.box_name,
         b.accept_group_codes, b.accepted_phone_sizes, b.max_slots;

COMMENT ON VIEW v_box_dashboard IS
    'Cross-box summary: storage counts per box for admin dashboards.';
