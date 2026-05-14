-- Queue SQLite : jobs en attente + idempotence par txn_id.

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT UNIQUE NOT NULL,
    txn_id          TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    step_done       TEXT,
    ucrm_payment_id TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    next_attempt_at REAL NOT NULL,
    last_error      TEXT,
    worker_id       TEXT,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_next ON jobs(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_jobs_txn ON jobs(txn_id);

CREATE TABLE IF NOT EXISTS processed_payments (
    txn_id          TEXT PRIMARY KEY,
    ucrm_payment_id TEXT,
    job_id          TEXT,
    processed_at    REAL NOT NULL
);
