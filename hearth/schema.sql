-- Hearth database schema (SQLite).
-- Every statement is idempotent so the schema can be applied on each start.
-- Timestamps are UTC ISO-8601 strings; dates are local YYYY-MM-DD strings.

CREATE TABLE IF NOT EXISTS profiles (
    id                      INTEGER PRIMARY KEY,
    circle_code             TEXT    NOT NULL UNIQUE,
    elder_name              TEXT    NOT NULL,
    timezone                TEXT    NOT NULL DEFAULT 'UTC',
    routine_json            TEXT    NOT NULL,
    checkin_enabled         INTEGER NOT NULL DEFAULT 1,
    checkin_hour            INTEGER NOT NULL DEFAULT 12 CHECK (checkin_hour BETWEEN 0 AND 23),
    last_elder_activity_at  TEXT,
    created_at              TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    id               INTEGER PRIMARY KEY,
    profile_id       INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name             TEXT    NOT NULL,
    email            TEXT    NOT NULL DEFAULT '',
    role             TEXT    NOT NULL CHECK (role IN ('elder', 'caretaker', 'family', 'viewer')),
    passcode_hash    TEXT    NOT NULL,
    trusted_editor   INTEGER NOT NULL DEFAULT 0,
    receives_alerts  INTEGER NOT NULL DEFAULT 1,
    session_version  INTEGER NOT NULL DEFAULT 1,
    active           INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_members_profile ON members(profile_id, active);

CREATE TABLE IF NOT EXISTS medications (
    id                INTEGER PRIMARY KEY,
    profile_id        INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name              TEXT    NOT NULL,
    dose              TEXT    NOT NULL DEFAULT '',
    instructions      TEXT    NOT NULL DEFAULT '',
    missed_dose_rule  TEXT    NOT NULL DEFAULT '',
    criticality       TEXT    NOT NULL DEFAULT 'important'
                      CHECK (criticality IN ('critical', 'important', 'routine')),
    pill_color        TEXT    NOT NULL DEFAULT 'white',
    pill_shape        TEXT    NOT NULL DEFAULT 'round',
    stock_count       INTEGER CHECK (stock_count IS NULL OR stock_count >= 0),
    units_per_dose    INTEGER NOT NULL DEFAULT 1 CHECK (units_per_dose >= 1),
    start_date        TEXT    NOT NULL,
    end_date          TEXT,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_medications_profile ON medications(profile_id, active);

-- One row per daily time a medicine is taken. Rows are retired, never edited,
-- so past doses keep pointing at the schedule that was true at the time.
CREATE TABLE IF NOT EXISTS schedules (
    id              INTEGER PRIMARY KEY,
    medication_id   INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
    anchor          TEXT    NOT NULL
                    CHECK (anchor IN ('wake', 'breakfast', 'lunch', 'dinner', 'bedtime', 'fixed')),
    offset_minutes  INTEGER NOT NULL DEFAULT 0,
    fixed_time      TEXT,
    days_mask       TEXT    NOT NULL DEFAULT '1111111',
    created_at      TEXT    NOT NULL,
    retired_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedules_medication ON schedules(medication_id);

CREATE TABLE IF NOT EXISTS dose_logs (
    id             INTEGER PRIMARY KEY,
    profile_id     INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    schedule_id    INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    dose_date      TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN ('taken', 'skipped', 'snoozed')),
    snoozed_until  TEXT,
    acted_at       TEXT    NOT NULL,
    acted_by       INTEGER REFERENCES members(id) ON DELETE SET NULL,
    UNIQUE (schedule_id, dose_date)
);
CREATE INDEX IF NOT EXISTS idx_dose_logs_profile_date ON dose_logs(profile_id, dose_date);

CREATE TABLE IF NOT EXISTS appointments (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    doctor      TEXT    NOT NULL DEFAULT '',
    location    TEXT    NOT NULL DEFAULT '',
    starts_at   TEXT    NOT NULL,
    notes       TEXT    NOT NULL DEFAULT '',
    cancelled   INTEGER NOT NULL DEFAULT 0,
    created_by  INTEGER REFERENCES members(id) ON DELETE SET NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appointments_profile_start ON appointments(profile_id, starts_at);

-- A proposed change to the elder's medicines, waiting for the elder's decision.
CREATE TABLE IF NOT EXISTS change_requests (
    id             INTEGER PRIMARY KEY,
    profile_id     INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    medication_id  INTEGER REFERENCES medications(id) ON DELETE CASCADE,
    action         TEXT    NOT NULL CHECK (action IN ('create', 'update', 'archive')),
    payload_json   TEXT    NOT NULL DEFAULT '',
    reason         TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'approved', 'declined')),
    proposed_by    INTEGER REFERENCES members(id) ON DELETE SET NULL,
    decided_by     INTEGER REFERENCES members(id) ON DELETE SET NULL,
    created_at     TEXT    NOT NULL,
    decided_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_requests_profile ON change_requests(profile_id, status);

CREATE TABLE IF NOT EXISTS journal_entries (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    member_id   INTEGER REFERENCES members(id) ON DELETE SET NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('symptom', 'question', 'note')),
    body        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_profile ON journal_entries(profile_id, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    member_id   INTEGER REFERENCES members(id) ON DELETE SET NULL,
    action      TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_profile ON audit_log(profile_id, created_at);

-- Every email Hearth sends (or would send, in demo mode). The unique key makes
-- the reminder job safe to run as often as you like: nothing is sent twice.
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    dedupe_key  TEXT    NOT NULL,
    recipient   TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'sent', 'outbox', 'failed')),
    error       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    UNIQUE (dedupe_key, recipient)
);
CREATE INDEX IF NOT EXISTS idx_notifications_profile ON notifications(profile_id, created_at);

CREATE TABLE IF NOT EXISTS dismissed_suggestions (
    profile_id    INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    key           TEXT    NOT NULL,
    dismissed_at  TEXT    NOT NULL,
    PRIMARY KEY (profile_id, key)
);

-- Failed sign-in counters, used to lock out passcode guessing.
CREATE TABLE IF NOT EXISTS throttle (
    bucket        TEXT PRIMARY KEY,
    attempts      INTEGER NOT NULL DEFAULT 0,
    window_start  TEXT    NOT NULL,
    locked_until  TEXT
);
