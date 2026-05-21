-- ============================================================
-- PhoneBox — Complete Database Schema
-- Canonical state AFTER migrations 0001 (multi-box) + 0002 (student groups)
--
-- Run this against a FRESH empty PhoneBoxDB:
--   psql -d PhoneBoxDB -f DBQuery.sql
--
-- For an existing database run migrations in order instead:
--   psql -d PhoneBoxDB -f migrations/0001_multi_box.sql
--   psql -d PhoneBoxDB -f migrations/0002_student_groups.sql
--
-- Optimizations embedded:
--   Opt #25  LISTEN/NOTIFY trigger for Python cache invalidation
--   Opt #35  Partial unique indexes on phone_storage
--   Opt #36a pgvector vector(96) + HNSW index on slot_baselines
--   Opt #36b pg_trgm GIN indexes on student names (fast ILIKE)
--   Opt #36c stored_at index on phone_storage (activity-report range queries)
-- ============================================================


-- ============================================================
-- EXTENSIONS
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;    -- Opt #36a: pgvector ANN
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- Opt #36b: trigram ILIKE


-- ============================================================
-- 1. BOXES  (physical cabinet registry)
-- ============================================================
-- Each box is one physical cabinet, identified at runtime by the
-- PHONEBOX_BOX_SLUG env var on the Flask process.
--
-- accept_group_codes   TEXT[]  — group codes allowed in this box.
--                               NULL = any student.
--                               Intersection with student effective groups
--                               must be non-empty for access (Phase 3).
--
-- accepted_phone_sizes TEXT[]  — sizes this cabinet holds physically.
--                               NULL = any size.
--                               e.g. '{standard}', '{large}', '{standard,large}'
-- ============================================================

CREATE TABLE IF NOT EXISTS boxes (
    box_id               SERIAL        PRIMARY KEY,
    box_slug             TEXT          NOT NULL UNIQUE,
    box_name             TEXT          NOT NULL,
    max_slots            INT           NOT NULL DEFAULT 30,
    accept_group_codes   TEXT[]        DEFAULT NULL,
    accepted_phone_sizes TEXT[]        DEFAULT NULL,
    created_at           TIMESTAMPTZ   DEFAULT NOW()
);

COMMENT ON TABLE  boxes                        IS 'Registry of physical phone-storage cabinets.';
COMMENT ON COLUMN boxes.box_slug               IS 'Matches PHONEBOX_BOX_SLUG env var on the server.';
COMMENT ON COLUMN boxes.accept_group_codes     IS 'NULL = any student. Group codes that may use this box (Phase 3).';
COMMENT ON COLUMN boxes.accepted_phone_sizes   IS 'NULL = any size. Physical hardware constraint: standard/large/extra_large.';

-- GIN indexes for array containment queries
CREATE INDEX IF NOT EXISTS idx_boxes_accept_groups
    ON boxes USING GIN(accept_group_codes);
CREATE INDEX IF NOT EXISTS idx_boxes_accept_sizes
    ON boxes USING GIN(accepted_phone_sizes);

-- Seed the default box for single-box deployments.
INSERT INTO boxes (box_id, box_slug, box_name, max_slots)
VALUES (1, 'box_1', 'Default Box', 30)
ON CONFLICT (box_slug) DO NOTHING;

SELECT setval('boxes_box_id_seq', GREATEST(1, (SELECT MAX(box_id) FROM boxes)));


-- ============================================================
-- 2. STUDENTS
-- ============================================================
-- year_code       TEXT    — root group code (e.g. 'year1', 'year2').
--                           One value only. NULL = unrestricted/staff.
--
-- sub_group_codes TEXT[]  — optional subcategory codes within the year
--                           (e.g. '{year1.1, year1.boys}').
--                           NULL or empty = access to ALL subcategories
--                           of year_code.
-- ============================================================

CREATE TABLE IF NOT EXISTS students (
    sid             CHAR(5)       PRIMARY KEY
                                  CHECK (sid ~ '^E[0-9]{4}$' AND char_length(sid) = 5),
    last_name       VARCHAR(40)   NOT NULL,
    first_name      VARCHAR(80)   NOT NULL,
    embed           FLOAT8[]      NOT NULL,
    year_code       TEXT          DEFAULT NULL,
    sub_group_codes TEXT[]        DEFAULT NULL,
    created_at      TIMESTAMPTZ   DEFAULT NOW(),
    modified_at     TIMESTAMPTZ   DEFAULT NOW()
);

COMMENT ON COLUMN students.year_code       IS 'Root group code (e.g. ''year1''). NULL = unrestricted/staff.';
COMMENT ON COLUMN students.sub_group_codes IS 'Subcategory codes within year_code. NULL/empty = all subcategories.';

-- Opt #36b: trigram GIN — turns ILIKE name search into indexed lookup
CREATE INDEX IF NOT EXISTS idx_students_fname_trgm
    ON students USING GIN(first_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_students_lname_trgm
    ON students USING GIN(last_name gin_trgm_ops);

-- Group-code indexes for access-control queries
CREATE INDEX IF NOT EXISTS idx_students_year_code
    ON students(year_code);
CREATE INDEX IF NOT EXISTS idx_students_sub_groups
    ON students USING GIN(sub_group_codes);


-- ============================================================
-- 3. LOCATIONS  (static physical slot grid, per box)
-- ============================================================
-- lid       SERIAL — globally unique slot ID (used everywhere as the FK)
-- box_id    INT    — which cabinet this slot belongs to
-- (box_id, x, y)  — unique per cabinet; two boxes can share same coordinates
-- ============================================================

CREATE TABLE IF NOT EXISTS locations (
    lid    SERIAL    PRIMARY KEY,
    x      SMALLINT  NOT NULL CHECK (x BETWEEN 1 AND 500),
    y      SMALLINT  NOT NULL CHECK (y BETWEEN 1 AND 500),
    box_id INT       NOT NULL DEFAULT 1 REFERENCES boxes(box_id),
    UNIQUE (box_id, x, y)
);

-- Reset sequence to start at 0 (slots are 0-indexed in Python)
ALTER SEQUENCE locations_lid_seq MINVALUE 0 START 0 RESTART 0;

COMMENT ON TABLE  locations        IS 'Static physical slot grid — one row per slot.';
COMMENT ON COLUMN locations.box_id IS 'Which physical cabinet this slot belongs to.';


-- ============================================================
-- 4. PHONES
-- ============================================================
-- phone_size  TEXT — 'standard' | 'large' | 'extra_large'
--   Determines which cabinets can physically accept this phone
--   (boxes.accepted_phone_sizes). Checked at deposit time.
-- ============================================================

CREATE TABLE IF NOT EXISTS phones (
    pid         UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    sid         CHAR(5)       NOT NULL
                              REFERENCES students(sid)
                              ON UPDATE CASCADE ON DELETE CASCADE,
    model       VARCHAR(50)   NOT NULL,
    imei        VARCHAR(20)   UNIQUE NOT NULL,
    cond        VARCHAR(30)
                              CHECK (cond IN ('New','Good','Fair','Damaged','Broken')
                                     OR cond IS NULL),
    admin_note  TEXT,
    stud_note   TEXT,
    phone_size  TEXT          NOT NULL DEFAULT 'standard'
                              CHECK (phone_size IN ('standard', 'large', 'extra_large')),
    created_at  TIMESTAMPTZ   DEFAULT NOW(),
    modified_at TIMESTAMPTZ   DEFAULT NOW()
);

COMMENT ON COLUMN phones.phone_size IS '''standard'' | ''large'' | ''extra_large''. Physical size used to match boxes.accepted_phone_sizes.';

CREATE INDEX IF NOT EXISTS idx_phones_sid  ON phones(sid);
CREATE INDEX IF NOT EXISTS idx_phones_imei ON phones(imei);


-- ============================================================
-- 5. PHONE_STORAGE  (which phone is in which slot right now)
-- ============================================================
-- box_id  INT  — denormalized from locations.box_id for fast per-box queries.
--               Backfilled from locations at deposit time by Python.
--               Lets dashboard queries avoid a JOIN to locations.
-- ============================================================

CREATE TABLE IF NOT EXISTS phone_storage (
    id           SERIAL       PRIMARY KEY,
    pid          UUID         NOT NULL REFERENCES phones(pid) ON DELETE CASCADE,
    lid          INT          NOT NULL REFERENCES locations(lid) ON DELETE CASCADE,
    stored_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    retrieved_at TIMESTAMPTZ,
    box_id       INT          REFERENCES boxes(box_id),   -- denormalized
    CONSTRAINT chk_retrieval_after_storage
        CHECK (retrieved_at IS NULL OR retrieved_at > stored_at)
);

COMMENT ON TABLE  phone_storage              IS 'Active + historical slot assignments. 1-month retention on retrieved rows.';
COMMENT ON COLUMN phone_storage.retrieved_at IS 'NULL = currently stored.';
COMMENT ON COLUMN phone_storage.box_id       IS 'Denormalized from locations.box_id. Set by Python at deposit time.';

-- Opt #35: partial unique indexes — O(1) active-record constraint checks
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_phone_storage
    ON phone_storage(pid) WHERE retrieved_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_location
    ON phone_storage(lid) WHERE retrieved_at IS NULL;

-- General access
CREATE INDEX IF NOT EXISTS idx_phone_storage_pid
    ON phone_storage(pid);
CREATE INDEX IF NOT EXISTS idx_phone_storage_lid
    ON phone_storage(lid);
CREATE INDEX IF NOT EXISTS idx_phone_storage_active
    ON phone_storage(pid, lid) WHERE retrieved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_phone_storage_retrieved
    ON phone_storage(retrieved_at) WHERE retrieved_at IS NOT NULL;

-- Opt #36c: date-range scans for activity reports
CREATE INDEX IF NOT EXISTS idx_phone_storage_stored_at
    ON phone_storage(stored_at);

-- Per-box dashboard / history queries
CREATE INDEX IF NOT EXISTS idx_phone_storage_box
    ON phone_storage(box_id);
CREATE INDEX IF NOT EXISTS idx_phone_storage_box_active
    ON phone_storage(box_id) WHERE retrieved_at IS NULL;


-- ============================================================
-- 6. SLOT_BASELINES  (visual embeddings for each physical slot)
-- ============================================================
-- Dual-column design (Opt #36a):
--   embedding     BYTEA      — always written; used by all DB reads
--   embedding_vec vector(96) — written by Python when pgvector is active;
--                              starts NULL, fills in on first save_baseline()
--
-- box_id INT — denormalized from locations.box_id so Python can filter
--              baselines for its own box without joining locations.
-- ============================================================

CREATE TABLE IF NOT EXISTS slot_baselines (
    lid           INT         PRIMARY KEY REFERENCES locations(lid) ON DELETE CASCADE,
    embedding     BYTEA       NOT NULL,
    embedding_vec vector(96),
    calibrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    box_id        INT         NOT NULL DEFAULT 1 REFERENCES boxes(box_id)
);

COMMENT ON COLUMN slot_baselines.embedding_vec IS 'Opt #36a vector. NULL until Python writes it. HNSW index covers only non-NULL rows.';
COMMENT ON COLUMN slot_baselines.box_id        IS 'Denormalized from locations.box_id for fast per-box baseline queries.';

-- Opt #36a: HNSW approximate-nearest-neighbour index (cosine, partial)
CREATE INDEX IF NOT EXISTS idx_slot_baselines_hnsw
    ON slot_baselines USING hnsw (embedding_vec vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE embedding_vec IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_slot_baselines_box
    ON slot_baselines(box_id);


-- ============================================================
-- 7. PHONE_OPERATIONS  (audit log, 1-month retention)
-- ============================================================

CREATE TABLE IF NOT EXISTS phone_operations (
    id        SERIAL       PRIMARY KEY,
    pid       UUID         NOT NULL REFERENCES phones(pid) ON DELETE CASCADE,
    operation VARCHAR(20)  NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    old_data  JSONB,
    new_data  JSONB,
    timestamp TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_phone_operations_pid_ts
    ON phone_operations(pid, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_phone_operations_ts
    ON phone_operations(timestamp DESC);


-- ============================================================
-- 8. EVIDENCE SYSTEM
-- ============================================================

CREATE TABLE IF NOT EXISTS evidence_sessions (
    id         SERIAL        PRIMARY KEY,
    session_id VARCHAR(8)    NOT NULL UNIQUE,
    opened_at  TIMESTAMPTZ   NOT NULL,
    closed_at  TIMESTAMPTZ,
    outcome    VARCHAR(20),
    warnings   JSONB,
    kept       BOOLEAN       NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_evidence_sessions_kept
    ON evidence_sessions(kept) WHERE kept = TRUE;

CREATE TABLE IF NOT EXISTS evidence_items (
    id          SERIAL       PRIMARY KEY,
    session_id  VARCHAR(8)   NOT NULL
                             REFERENCES evidence_sessions(session_id)
                             ON DELETE CASCADE,
    captured_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    event_type  VARCHAR(50)  NOT NULL,
    lid         INTEGER,
    -- pid is VARCHAR not UUID: may be 'pending-lid{N}' before QR confirmed
    pid         VARCHAR(50),
    photo_path  VARCHAR(500),
    metadata    JSONB
);

CREATE INDEX IF NOT EXISTS idx_evidence_items_session ON evidence_items(session_id);
CREATE INDEX IF NOT EXISTS idx_evidence_items_event   ON evidence_items(event_type);


-- ============================================================
-- FUNCTIONS
-- ============================================================

-- Keep modified_at current on every UPDATE
CREATE OR REPLACE FUNCTION update_modified_column()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.modified_at = NOW();
    RETURN NEW;
END;
$$;

-- Guarantee phones always get a UUID pid (safety net for direct SQL inserts)
CREATE OR REPLACE FUNCTION phones_defaults()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.pid IS NULL THEN
        NEW.pid := gen_random_uuid();
    END IF;
    RETURN NEW;
END;
$$;

-- Append row to audit log on every phones change
CREATE OR REPLACE FUNCTION log_phone_operation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    op_type  VARCHAR(20);
    old_json JSONB;
    new_json JSONB;
BEGIN
    CASE TG_OP
        WHEN 'INSERT' THEN op_type := 'INSERT'; old_json := NULL;          new_json := to_jsonb(NEW);
        WHEN 'UPDATE' THEN op_type := 'UPDATE'; old_json := to_jsonb(OLD); new_json := to_jsonb(NEW);
        WHEN 'DELETE' THEN op_type := 'DELETE'; old_json := to_jsonb(OLD); new_json := NULL;
    END CASE;
    INSERT INTO phone_operations(pid, operation, old_data, new_data, timestamp)
    VALUES (COALESCE(NEW.pid, OLD.pid), op_type, old_json, new_json, NOW());
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

-- Auto-assign the first free lid in NEW.box_id when lid IS NULL on INSERT.
-- Python always passes lid explicitly; this is the safety net for direct SQL.
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
    'Assigns the first free slot in NEW.box_id when NEW.lid IS NULL. Multi-box aware.';

-- Opt #25: broadcast lid via LISTEN/NOTIFY after every phone_storage change.
-- Python AsyncSlotMonitorDB listens on 'phonebox_storage_change' and
-- invalidates its pid→lid cache in <1 ms.
CREATE OR REPLACE FUNCTION notify_storage_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify(
        'phonebox_storage_change',
        CASE WHEN TG_OP = 'DELETE' THEN OLD.lid::text ELSE NEW.lid::text END
    );
    RETURN NULL;  -- AFTER trigger; return value ignored
END;
$$;

-- Delete audit + completed storage rows older than 1 month.
-- Schedule weekly via pg_cron or OS cron.
CREATE OR REPLACE FUNCTION cleanup_old_logs()
RETURNS TABLE(deleted_phone_ops INT, deleted_storage INT)
LANGUAGE plpgsql AS $$
DECLARE
    d_ops  INT;
    d_stor INT;
BEGIN
    DELETE FROM phone_operations  WHERE timestamp    < NOW() - INTERVAL '1 month';
    GET DIAGNOSTICS d_ops  = ROW_COUNT;

    DELETE FROM phone_storage
    WHERE retrieved_at IS NOT NULL
      AND retrieved_at < NOW() - INTERVAL '1 month';
    GET DIAGNOSTICS d_stor = ROW_COUNT;

    deleted_phone_ops := d_ops;
    deleted_storage   := d_stor;
    RETURN NEXT;

    RAISE NOTICE 'Cleanup: % phone_ops + % storage rows deleted', d_ops, d_stor;
END;
$$;

-- Return first N empty locations for a box (ordered by lid)
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

-- Return all (lid, x, y) rows for a box — used by ROI calibration and
-- slot-monitor startup to get real lid values (not assumed 0-based).
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
    'Returns all (lid, x, y) for a given box, ordered by lid. '
    'Used by ROI calibration and slot monitor startup.';

-- Full storage history for one phone (most recent first, capped at N months)
CREATE OR REPLACE FUNCTION get_phone_storage_history(
    phone_pid   UUID,
    months_back INT DEFAULT 1
)
RETURNS TABLE(
    storage_id   INT,
    lid          INT,
    x            SMALLINT,
    y            SMALLINT,
    stored_at    TIMESTAMPTZ,
    retrieved_at TIMESTAMPTZ,
    duration     INTERVAL
)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT ps.id, ps.lid, l.x, l.y, ps.stored_at, ps.retrieved_at,
           COALESCE(ps.retrieved_at, NOW()) - ps.stored_at AS duration
    FROM   phone_storage ps
    JOIN   locations l ON ps.lid = l.lid
    WHERE  ps.pid       = phone_pid
      AND  ps.stored_at > NOW() - (months_back || ' months')::INTERVAL
    ORDER  BY ps.stored_at DESC;
END;
$$;

-- Full usage history for one physical slot
CREATE OR REPLACE FUNCTION get_location_usage_history(
    location_lid INT,
    months_back  INT DEFAULT 1
)
RETURNS TABLE(
    phone_pid    UUID,
    imei         VARCHAR,
    student_sid  CHAR,
    stored_at    TIMESTAMPTZ,
    retrieved_at TIMESTAMPTZ,
    duration     INTERVAL
)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT ps.pid, p.imei, p.sid, ps.stored_at, ps.retrieved_at,
           COALESCE(ps.retrieved_at, NOW()) - ps.stored_at AS duration
    FROM   phone_storage ps
    JOIN   phones p ON ps.pid = p.pid
    WHERE  ps.lid       = location_lid
      AND  ps.stored_at > NOW() - (months_back || ' months')::INTERVAL
    ORDER  BY ps.stored_at DESC;
END;
$$;


-- ============================================================
-- TRIGGERS
-- ============================================================

-- phones: guarantee UUID pid before INSERT
CREATE TRIGGER phones_before_insert_defaults
    BEFORE INSERT ON phones
    FOR EACH ROW EXECUTE FUNCTION phones_defaults();

-- phones: keep modified_at current
CREATE TRIGGER phones_before_update
    BEFORE UPDATE ON phones
    FOR EACH ROW EXECUTE FUNCTION update_modified_column();

-- phones: write audit log
CREATE TRIGGER phones_after_all_operations
    AFTER INSERT OR UPDATE OR DELETE ON phones
    FOR EACH ROW EXECUTE FUNCTION log_phone_operation();

-- students: keep modified_at current
CREATE TRIGGER students_before_update
    BEFORE UPDATE ON students
    FOR EACH ROW EXECUTE FUNCTION update_modified_column();

-- phone_storage: auto-assign empty lid when lid IS NULL on INSERT
CREATE TRIGGER phone_storage_before_insert_auto_lid
    BEFORE INSERT ON phone_storage
    FOR EACH ROW EXECUTE FUNCTION auto_assign_location();

-- Opt #25: broadcast every phone_storage change for Python cache invalidation
CREATE TRIGGER phone_storage_notify_change
    AFTER INSERT OR UPDATE OR DELETE ON phone_storage
    FOR EACH ROW EXECUTE FUNCTION notify_storage_change();


-- ============================================================
-- VIEWS
-- ============================================================

-- All phones currently in storage with full student + box + location context
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
    l.x,
    l.y,
    st.first_name,
    st.last_name,
    st.year_code,
    st.sub_group_codes
FROM phone_storage ps
JOIN phones    p  ON ps.pid    = p.pid
JOIN locations l  ON ps.lid    = l.lid
JOIN boxes     b  ON ps.box_id = b.box_id
JOIN students  st ON p.sid     = st.sid
WHERE ps.retrieved_at IS NULL;

COMMENT ON VIEW v_active_phone_storage IS
    'All phones currently in storage with full student, box, and location context.';


-- All unoccupied slots with box context
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

COMMENT ON VIEW v_empty_locations IS 'All unoccupied slots with box context.';


-- Cross-box fill summary for admin dashboard
CREATE OR REPLACE VIEW v_box_dashboard AS
SELECT
    b.box_id,
    b.box_slug,
    b.box_name,
    b.accept_group_codes,
    b.accepted_phone_sizes,
    b.max_slots,
    COUNT(ps.id) FILTER (WHERE ps.retrieved_at IS NULL)                          AS phones_stored,
    b.max_slots - COUNT(ps.id) FILTER (WHERE ps.retrieved_at IS NULL)            AS slots_free,
    ROUND(
        COUNT(ps.id) FILTER (WHERE ps.retrieved_at IS NULL)::NUMERIC
        / NULLIF(b.max_slots, 0) * 100, 1
    )                                                                             AS fill_pct
FROM boxes b
LEFT JOIN locations l  ON l.box_id = b.box_id
LEFT JOIN phone_storage ps ON ps.lid = l.lid
GROUP BY b.box_id, b.box_slug, b.box_name,
         b.accept_group_codes, b.accepted_phone_sizes, b.max_slots
ORDER BY b.box_id;

COMMENT ON VIEW v_box_dashboard IS
    'Cross-box fill summary — used by central dashboard endpoint.';


-- ============================================================
-- COMMENTS
-- ============================================================

COMMENT ON TABLE students         IS 'Registered students with face embedding.';
COMMENT ON TABLE locations        IS 'Static physical slot grid — one row per slot.';
COMMENT ON TABLE phones           IS 'Student phones. phone_size drives cabinet acceptance.';
COMMENT ON TABLE phone_storage    IS 'Active + historical slot assignments. 1-month retention on retrieved rows. lid auto-assigned when NULL.';
COMMENT ON TABLE phone_operations IS 'Audit log for phones table. 1-month retention.';
COMMENT ON TABLE slot_baselines   IS 'Per-slot visual embedding. embedding=BYTEA always; embedding_vec=vector(96) written by Python (Opt #36a).';
COMMENT ON TABLE evidence_sessions IS 'One row per admin resolution session.';
COMMENT ON TABLE evidence_items    IS 'One row per video clip / event within a session.';

COMMENT ON INDEX uniq_active_phone_storage IS 'Opt #35: prevents double-deposit of same phone.';
COMMENT ON INDEX uniq_active_location      IS 'Opt #35: prevents two phones in one slot.';
COMMENT ON INDEX idx_students_fname_trgm   IS 'Opt #36b: GIN trigram — fast ILIKE name search.';
COMMENT ON INDEX idx_slot_baselines_hnsw   IS 'Opt #36a: HNSW cosine ANN on 96-dim embeddings (partial: WHERE embedding_vec IS NOT NULL).';
COMMENT ON INDEX idx_phone_storage_box_active IS 'Fast per-box active-phone queries for dashboard and slot monitor.';


-- ============================================================
-- SCHEDULED CLEANUP  (run once after first deploy)
-- ============================================================
-- Option A — pg_cron (recommended):
--   CREATE EXTENSION pg_cron;
--   SELECT cron.schedule('phonebox-cleanup', '0 2 * * 0',
--       $$SELECT cleanup_old_logs()$$);
--
-- Option B — OS cron:
--   0 2 * * 0  psql -d PhoneBoxDB -c "SELECT cleanup_old_logs();"


-- ============================================================
-- INITIAL BOX SETUP  (fill in real slugs for your deployment)
-- ============================================================
--
-- INSERT INTO boxes (box_slug, box_name, max_slots,
--                    accept_group_codes, accepted_phone_sizes)
-- VALUES
--   ('year_1', 'Year 1 Box',      30, '{year1}',         '{standard}'),
--   ('year_2', 'Year 2 Box',      30, '{year2}',         '{standard}'),
--   ('year_3', 'Year 3 Box',      30, '{year3}',         '{standard}'),
--   ('large',  'Large Phone Box', 20, NULL,               '{large,extra_large}'),
--   ('shared', 'Shared Box',      30, NULL,               NULL);
--
-- Then populate locations for each box (e.g. 5×6 grid):
--   INSERT INTO locations (x, y, box_id)
--   SELECT x, y, (SELECT box_id FROM boxes WHERE box_slug = 'year_1')
--   FROM generate_series(1,5) x, generate_series(1,6) y;
