-- ============================================================
-- Migration 0001: Multi-box support
-- ============================================================
-- Run ONCE against PhoneBoxDB after verifying on a dev copy.
-- Safe to run on an existing populated database.
--
-- Apply with:
--   psql -d PhoneBoxDB -f migrations/0001_multi_box.sql
--
-- What this migration does:
--   1.  Creates the boxes table (physical cabinet registry)
--   2.  Adds box_id to locations (slot grid is now per-box)
--       Replaces UNIQUE(x,y) with UNIQUE(box_id, x, y)
--   3.  Adds box_id to slot_baselines (baseline scoped per box)
--   4.  Adds box_id to phone_storage (for reporting / dashboard)
--   5.  Adds year_group to students
--   6.  Adds allowed_box_slugs + phone_size to phones
--   7.  Updates auto_assign_location() to respect box_id
--   8.  Updates get_available_locations() with optional box filter
--   9.  Adds get_box_lids() — returns actual lid values for a box
--       (used by ROI calibration and slot monitor startup)
--   10. Refreshes v_active_phone_storage and v_empty_locations views
--
-- Backward compatibility:
--   All existing rows are assigned to a default box (box_id = 1,
--   slug = 'box_1').  Single-box deployments continue to work without
--   any server environment change: BOX_SLUG defaults to 'box_1'.
-- ============================================================

BEGIN;

-- ============================================================
-- 1. BOXES TABLE
-- ============================================================

CREATE TABLE IF NOT EXISTS boxes (
    box_id              SERIAL        PRIMARY KEY,
    box_slug            TEXT          NOT NULL UNIQUE,
    box_name            TEXT          NOT NULL,
    max_slots           INT           NOT NULL DEFAULT 30,
    -- accept_year_groups: NULL = accept any year group
    --   Examples: '{1}' = Year 1 only, '{1,2}' = Year 1 or 2
    accept_year_groups  SMALLINT[]    DEFAULT NULL,
    created_at          TIMESTAMPTZ   DEFAULT NOW()
);

COMMENT ON TABLE boxes IS
    'Registry of physical phone-storage cabinets. '
    'Each cabinet runs one Flask process identified by box_slug.';
COMMENT ON COLUMN boxes.box_slug IS
    'Matches the PHONEBOX_BOX_SLUG environment variable on the server.';
COMMENT ON COLUMN boxes.accept_year_groups IS
    'NULL = any student. {1} = year 1 only. {1,2} = year 1 or 2.';

-- Seed the default box for existing single-box deployments.
-- INSERT is skipped if box_slug already exists (idempotent on re-run).
INSERT INTO boxes (box_id, box_slug, box_name, max_slots, accept_year_groups)
VALUES (1, 'box_1', 'Default Box', 30, NULL)
ON CONFLICT (box_slug) DO NOTHING;

-- Keep the SERIAL sequence ahead of the manual insert
SELECT setval('boxes_box_id_seq', GREATEST(1, (SELECT MAX(box_id) FROM boxes)));


-- ============================================================
-- 2. LOCATIONS — add box_id, fix unique constraint
-- ============================================================

ALTER TABLE locations
    ADD COLUMN IF NOT EXISTS box_id INT NOT NULL DEFAULT 1
    REFERENCES boxes(box_id);

-- Backfill existing rows to the default box (already DEFAULT 1,
-- but explicit for clarity and idempotency).
UPDATE locations SET box_id = 1 WHERE box_id IS NULL OR box_id = 1;

-- Replace the global UNIQUE(x, y) with a per-box UNIQUE(box_id, x, y).
-- Two different boxes CAN share the same (x, y) grid coordinates.
ALTER TABLE locations DROP CONSTRAINT IF EXISTS locations_x_y_key;
ALTER TABLE locations
    ADD CONSTRAINT uq_locations_box_xy UNIQUE (box_id, x, y);

COMMENT ON COLUMN locations.box_id IS
    'Which physical cabinet this slot belongs to.';


-- ============================================================
-- 3. SLOT_BASELINES — add box_id
-- ============================================================
-- The lid column remains the PRIMARY KEY (it references locations.lid,
-- which is globally unique via SERIAL).  We add box_id as a redundant
-- denormalised column so the Python layer can filter baselines for its
-- own box without joining to locations every time.

ALTER TABLE slot_baselines
    ADD COLUMN IF NOT EXISTS box_id INT NOT NULL DEFAULT 1
    REFERENCES boxes(box_id);

-- Backfill from locations
UPDATE slot_baselines sb
SET box_id = l.box_id
FROM locations l
WHERE l.lid = sb.lid
  AND sb.box_id IS DISTINCT FROM l.box_id;

-- Index: fetch all baselines for a box efficiently
CREATE INDEX IF NOT EXISTS idx_slot_baselines_box
    ON slot_baselines(box_id);

COMMENT ON COLUMN slot_baselines.box_id IS
    'Denormalised from locations.box_id for fast per-box baseline queries.';


-- ============================================================
-- 4. PHONE_STORAGE — add box_id
-- ============================================================

ALTER TABLE phone_storage
    ADD COLUMN IF NOT EXISTS box_id INT REFERENCES boxes(box_id);

-- Backfill from locations (derive which box stored each phone)
UPDATE phone_storage ps
SET box_id = l.box_id
FROM locations l
WHERE l.lid = ps.lid
  AND ps.box_id IS NULL;

-- Index: fast dashboard queries (count per box, history per box)
CREATE INDEX IF NOT EXISTS idx_phone_storage_box
    ON phone_storage(box_id);

CREATE INDEX IF NOT EXISTS idx_phone_storage_box_active
    ON phone_storage(box_id)
    WHERE retrieved_at IS NULL;

COMMENT ON COLUMN phone_storage.box_id IS
    'Denormalised: which cabinet this storage event occurred in. '
    'Derived from locations.box_id at deposit time.';


-- ============================================================
-- 5. STUDENTS — add year_group
-- ============================================================

ALTER TABLE students
    ADD COLUMN IF NOT EXISTS year_group SMALLINT DEFAULT NULL
    CHECK (year_group IS NULL OR year_group BETWEEN 1 AND 10);

COMMENT ON COLUMN students.year_group IS
    'Academic year group (1, 2, 3, ...). NULL = unrestricted / staff / admin.';


-- ============================================================
-- 6. PHONES — add type flags
-- ============================================================

ALTER TABLE phones
    ADD COLUMN IF NOT EXISTS phone_size        TEXT    NOT NULL DEFAULT 'standard'
        CHECK (phone_size IN ('standard', 'large')),
    ADD COLUMN IF NOT EXISTS allowed_box_slugs TEXT[]  DEFAULT NULL;

-- allowed_box_slugs:
--   NULL           → no restriction, phone may be deposited in any box
--   '{year_1}'     → only allowed in the 'year_1' box
--   '{year_1,shared}' → allowed in 'year_1' or 'shared' box

CREATE INDEX IF NOT EXISTS idx_phones_allowed_slugs
    ON phones USING GIN(allowed_box_slugs);

COMMENT ON COLUMN phones.phone_size IS
    '''standard'' (default) or ''large''. Set at phone registration.';
COMMENT ON COLUMN phones.allowed_box_slugs IS
    'NULL = any box. Non-null = deposit rejected in unlisted boxes.';


-- ============================================================
-- 7. UPDATE auto_assign_location() — respect box_id
-- ============================================================
-- When Python inserts into phone_storage with lid IS NULL, the trigger
-- auto-assigns the first free slot.  In a multi-box world it must only
-- pick a slot belonging to the same box (NEW.box_id).
--
-- Note: The Python layer always passes lid explicitly (it calls
-- get_next_free_lid() first).  This trigger is the safety net for
-- direct SQL inserts and edge cases.

CREATE OR REPLACE FUNCTION auto_assign_location()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.lid IS NULL THEN

        IF NEW.box_id IS NULL THEN
            RAISE EXCEPTION
                'phone_storage: cannot auto-assign lid — box_id must be set';
        END IF;

        SELECT l.lid INTO NEW.lid
        FROM   locations l
        WHERE  l.box_id = NEW.box_id
          AND  NOT EXISTS (
              SELECT 1 FROM phone_storage ps
              WHERE  ps.lid = l.lid AND ps.retrieved_at IS NULL
          )
        ORDER BY l.lid
        LIMIT 1
        FOR UPDATE SKIP LOCKED;   -- concurrency-safe

        IF NEW.lid IS NULL THEN
            RAISE EXCEPTION
                'No empty locations available in box_id=%', NEW.box_id;
        END IF;

    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION auto_assign_location() IS
    'Assigns the first free slot in NEW.box_id when NEW.lid IS NULL. '
    'Multi-box aware since migration 0001.';


-- ============================================================
-- 8. UPDATE get_available_locations() — add box_id filter
-- ============================================================

CREATE OR REPLACE FUNCTION get_available_locations(
    limit_count INT DEFAULT 10,
    for_box_id  INT DEFAULT NULL   -- NULL = any box (backward compat)
)
RETURNS TABLE(lid INT, x SMALLINT, y SMALLINT)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT l.lid, l.x, l.y
    FROM   locations l
    LEFT JOIN phone_storage ps ON l.lid = ps.lid AND ps.retrieved_at IS NULL
    WHERE  ps.lid IS NULL
    AND   (for_box_id IS NULL OR l.box_id = for_box_id)
    ORDER  BY l.lid
    LIMIT  limit_count;
END;
$$;


-- ============================================================
-- 9. NEW: get_box_lids() — actual lid values for a box
-- ============================================================
-- Used by Python ROI calibration and slot monitor startup to get
-- the REAL lid values (e.g. 100, 101, ..., 129 for box 2) instead
-- of assuming lids start at 0.

CREATE OR REPLACE FUNCTION get_box_lids(for_box_id INT)
RETURNS TABLE(lid INT, x SMALLINT, y SMALLINT)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT l.lid, l.x, l.y
    FROM   locations l
    WHERE  l.box_id = for_box_id
    ORDER  BY l.lid;
END;
$$;

COMMENT ON FUNCTION get_box_lids(INT) IS
    'Returns all (lid, x, y) rows for a given box, ordered by lid. '
    'Used by ROI calibration and slot monitor startup.';


-- ============================================================
-- 10. REFRESH VIEWS
-- ============================================================

-- v_active_phone_storage: add box context columns
CREATE OR REPLACE VIEW v_active_phone_storage AS
SELECT
    ps.id,
    ps.pid,
    ps.lid,
    ps.stored_at,
    ps.box_id,
    b.box_slug,
    b.box_name,
    p.sid,
    p.imei,
    p.model,
    p.cond,
    p.phone_size,
    p.allowed_box_slugs,
    l.x,
    l.y,
    st.first_name,
    st.last_name,
    st.year_group
FROM phone_storage ps
JOIN phones    p  ON ps.pid    = p.pid
JOIN locations l  ON ps.lid    = l.lid
JOIN boxes     b  ON ps.box_id = b.box_id
JOIN students  st ON p.sid     = st.sid
WHERE ps.retrieved_at IS NULL;

COMMENT ON VIEW v_active_phone_storage IS
    'All phones currently in storage with full student, box, and location context.';


-- v_empty_locations: add box context
CREATE OR REPLACE VIEW v_empty_locations AS
SELECT
    l.lid,
    l.x,
    l.y,
    l.box_id,
    b.box_slug,
    b.box_name
FROM   locations l
JOIN   boxes b ON l.box_id = b.box_id
LEFT JOIN phone_storage ps ON l.lid = ps.lid AND ps.retrieved_at IS NULL
WHERE  ps.lid IS NULL;

COMMENT ON VIEW v_empty_locations IS
    'All unoccupied slots with box context.';


-- ============================================================
-- DASHBOARD HELPER VIEW (cross-box summary)
-- ============================================================

CREATE OR REPLACE VIEW v_box_dashboard AS
SELECT
    b.box_id,
    b.box_slug,
    b.box_name,
    b.max_slots,
    b.accept_year_groups,
    COUNT(ps.id)                                        AS phones_stored,
    b.max_slots - COUNT(ps.id)                          AS slots_free,
    ROUND(COUNT(ps.id)::NUMERIC / b.max_slots * 100, 1) AS fill_pct
FROM boxes b
LEFT JOIN locations l ON l.box_id = b.box_id
LEFT JOIN phone_storage ps
       ON ps.lid = l.lid AND ps.retrieved_at IS NULL
GROUP BY b.box_id, b.box_slug, b.box_name, b.max_slots, b.accept_year_groups
ORDER BY b.box_id;

COMMENT ON VIEW v_box_dashboard IS
    'Cross-box fill summary — used by central dashboard endpoint.';


COMMIT;

-- ============================================================
-- POST-MIGRATION CHECKLIST (run manually, not part of this script)
-- ============================================================
-- 1. Add real box rows for your deployment:
--
--    INSERT INTO boxes (box_slug, box_name, max_slots, accept_year_groups)
--    VALUES
--      ('year_1', 'Year 1 Box', 30, '{1}'),
--      ('year_2', 'Year 2 Box', 30, '{2}'),
--      ('year_3', 'Year 3 Box', 30, '{3}'),
--      ('large',  'Large Phones Box', 20, NULL),
--      ('shared', 'Shared Box', 30, NULL);
--
-- 2. Populate locations for each box:
--
--    -- Example: 5×6 grid for Year 1 box
--    INSERT INTO locations (x, y, box_id)
--    SELECT x, y, (SELECT box_id FROM boxes WHERE box_slug = 'year_1')
--    FROM generate_series(1,5) x, generate_series(1,6) y;
--
-- 3. Set year_group on existing students (bulk update):
--
--    UPDATE students SET year_group = 1 WHERE sid LIKE 'E1%';
--    UPDATE students SET year_group = 2 WHERE sid LIKE 'E2%';
--
-- 4. Set allowed_box_slugs on existing phones (optional, NULL = unrestricted):
--
--    UPDATE phones SET allowed_box_slugs = '{year_1}'
--    WHERE sid IN (SELECT sid FROM students WHERE year_group = 1);
--
-- 5. Set phone_size = 'large' for oversized phones:
--
--    UPDATE phones SET phone_size = 'large' WHERE imei IN (...);
