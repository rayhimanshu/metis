-- Metis bus schema.
--
-- `events` is append-only and authoritative; everything else is either current
-- state derived from it or an index into it. This is the ledger-and-balance
-- rule: never store the balance as truth. It is also what makes concurrent
-- writers safe -- an append cannot overwrite another writer's row, whereas a
-- whole-record rewrite silently destroys whatever it did not know about.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    workspace      TEXT    NOT NULL,
    environment    TEXT    NOT NULL,
    requirement    TEXT    NOT NULL,
    max_iterations INTEGER NOT NULL DEFAULT 4,
    status         TEXT    NOT NULL DEFAULT 'RUNNING',  -- RUNNING | DONE | HALTED
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT    NOT NULL REFERENCES runs(id),
    ts          TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    agent       TEXT,
    target      TEXT,
    environment TEXT,
    iteration   INTEGER NOT NULL DEFAULT 1,
    payload     TEXT,

    -- Audit. Present from the first schema on purpose: causality cannot be
    -- reconstructed after the fact, so a column added later is a column that
    -- is empty for everything that already happened.
    tier        TEXT    NOT NULL DEFAULT 'testimony',   -- ground_truth | testimony
    caused_by   INTEGER REFERENCES events(id),
    session_id  TEXT,
    rationale   TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_run_id     ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type       ON events(run_id, type);
CREATE INDEX IF NOT EXISTS idx_events_target     ON events(run_id, target);
CREATE INDEX IF NOT EXISTS idx_events_caused_by  ON events(caused_by);

-- One row per held slot. A capacity-N key can hold up to N rows, so the
-- primary key is (key, slot) rather than key alone.
CREATE TABLE IF NOT EXISTS claims (
    key         TEXT    NOT NULL,
    slot        INTEGER NOT NULL,
    run_id      TEXT    NOT NULL REFERENCES runs(id),
    owner       TEXT    NOT NULL,
    pid         INTEGER,
    acquired_at TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL,
    PRIMARY KEY (key, slot)
);

CREATE INDEX IF NOT EXISTS idx_claims_owner ON claims(run_id, owner);

-- How far each agent has consumed the stream. Lets a session that was down
-- catch up rather than lose everything sent while it was away.
CREATE TABLE IF NOT EXISTS cursors (
    agent         TEXT    NOT NULL,
    run_id        TEXT    NOT NULL REFERENCES runs(id),
    last_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT    NOT NULL,
    PRIMARY KEY (agent, run_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES runs(id),
    ts         TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent   TEXT NOT NULL,
    subject    TEXT,
    body       TEXT,
    read_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(run_id, to_agent, read_at);

-- Diffs and captured output, referenced by an event. Kept out of the payload so
-- an event row stays small enough to read in a terminal.
CREATE TABLE IF NOT EXISTS artifacts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id),
    kind     TEXT    NOT NULL,          -- diff | log | report
    sha256   TEXT    NOT NULL,
    body     BLOB    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_event ON artifacts(event_id);
