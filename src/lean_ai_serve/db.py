"""SQLite persistence — model registry, API keys, audit log, usage tracking."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

_SCHEMA = """
-- Model registry
CREATE TABLE IF NOT EXISTS models (
    name            TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'not_downloaded',
    port            INTEGER,
    pid             INTEGER,
    gpu_assignment  TEXT,       -- JSON array
    config_json     TEXT,       -- Full ModelConfig as JSON
    downloaded_at   TEXT,
    loaded_at       TEXT,
    error_message   TEXT
);

-- API keys (bcrypt hashed)
CREATE TABLE IF NOT EXISTS api_keys (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    key_hash        TEXT NOT NULL,
    key_prefix      TEXT NOT NULL,  -- First 8 chars for identification
    role            TEXT NOT NULL DEFAULT 'user',
    models          TEXT NOT NULL DEFAULT '["*"]',  -- JSON array
    rate_limit      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    expires_at      TEXT,
    last_used_at    TEXT
);

-- Audit log (append-only, tamper-evident via hash chain)
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    user_role       TEXT NOT NULL DEFAULT '',
    source_ip       TEXT NOT NULL DEFAULT '',
    action          TEXT NOT NULL,
    model           TEXT,
    prompt_content  TEXT,       -- Full content or NULL if hash-only mode
    prompt_hash     TEXT,       -- SHA-256 hash of prompt
    response_content TEXT,      -- Full content or NULL if hash-only mode
    response_hash   TEXT,       -- SHA-256 hash of response
    token_count     INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'success',
    error_detail    TEXT,
    chain_hash      TEXT        -- SHA-256 of previous row for tamper detection
);

-- Usage tracking (aggregated per hour)
CREATE TABLE IF NOT EXISTS usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hour            TEXT NOT NULL,  -- ISO 8601 truncated to hour
    user_id         TEXT NOT NULL,
    model           TEXT NOT NULL,
    request_count   INTEGER NOT NULL DEFAULT 0,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_latency_ms INTEGER NOT NULL DEFAULT 0,
    UNIQUE(hour, user_id, model)
);

-- LoRA adapters
CREATE TABLE IF NOT EXISTS adapters (
    name            TEXT PRIMARY KEY,
    base_model      TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'available', -- available, deployed, error
    training_job_id TEXT,
    created_at      TEXT NOT NULL,
    deployed_at     TEXT,
    metadata_json   TEXT        -- Training metrics, config, etc.
);

-- Training jobs
CREATE TABLE IF NOT EXISTS training_jobs (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    base_model      TEXT NOT NULL,
    dataset         TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'queued',
    gpu             TEXT,           -- JSON array of GPU indices
    output_path     TEXT,
    adapter_name    TEXT,
    submitted_by    TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT,
    error_message   TEXT,
    metrics_json    TEXT            -- Loss, eval metrics, etc.
);

-- Datasets
CREATE TABLE IF NOT EXISTS datasets (
    name            TEXT PRIMARY KEY,
    path            TEXT NOT NULL,
    format          TEXT NOT NULL,  -- sharegpt, alpaca, jsonl, csv
    row_count       INTEGER,
    size_bytes      INTEGER,
    uploaded_by     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    metadata_json   TEXT
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_usage_hour ON usage(hour);
CREATE INDEX IF NOT EXISTS idx_training_state ON training_jobs(state);
"""


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the database and initialize schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("Database initialized at %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Return the active connection (raises if not connected)."""
        if self._db is None:
            raise RuntimeError("Database not connected — call connect() first")
        return self._db

    async def execute(
        self, sql: str, params: tuple | dict | None = None
    ) -> aiosqlite.Cursor:
        """Execute a single SQL statement."""
        return await self.conn.execute(sql, params or ())

    async def executemany(
        self, sql: str, params_seq: list[tuple | dict]
    ) -> aiosqlite.Cursor:
        """Execute a SQL statement for each parameter set."""
        return await self.conn.executemany(sql, params_seq)

    async def fetchone(
        self, sql: str, params: tuple | dict | None = None
    ) -> aiosqlite.Row | None:
        """Execute and fetch one row."""
        cursor = await self.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(
        self, sql: str, params: tuple | dict | None = None
    ) -> list[aiosqlite.Row]:
        """Execute and fetch all rows."""
        cursor = await self.execute(sql, params)
        return await cursor.fetchall()

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self.conn.commit()
