"""Database persistence — model registry, API keys, audit log, usage tracking.

Supports multiple backends via SQLAlchemy Core (async).  The default backend
is SQLite (via ``aiosqlite``).  Users can point to PostgreSQL, Oracle DB,
MySQL, etc. by setting ``database.url`` in the config YAML.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema definitions (SQLAlchemy Core)
# ---------------------------------------------------------------------------

metadata = sa.MetaData()

models_table = sa.Table(
    "models",
    metadata,
    sa.Column("name", sa.String(255), primary_key=True),
    sa.Column("source", sa.String(512), nullable=False),
    sa.Column("state", sa.String(32), nullable=False, server_default="not_downloaded"),
    sa.Column("port", sa.Integer),
    sa.Column("pid", sa.Integer),
    sa.Column("gpu_assignment", sa.Text),  # JSON array
    sa.Column("config_json", sa.Text),  # Full ModelConfig as JSON
    sa.Column("downloaded_at", sa.String(64)),
    sa.Column("loaded_at", sa.String(64)),
    sa.Column("error_message", sa.Text),
)

api_keys_table = sa.Table(
    "api_keys",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("key_hash", sa.String(255), nullable=False),
    sa.Column("key_prefix", sa.String(16), nullable=False),
    sa.Column("role", sa.String(32), nullable=False, server_default="user"),
    sa.Column("models", sa.Text, nullable=False, server_default='["*"]'),  # JSON array
    sa.Column("rate_limit", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("expires_at", sa.String(64)),
    sa.Column("last_used_at", sa.String(64)),
)

audit_log_table = sa.Table(
    "audit_log",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("timestamp", sa.String(64), nullable=False),
    sa.Column("request_id", sa.String(64), nullable=False),
    sa.Column("user_id", sa.String(255), nullable=False),
    sa.Column("user_role", sa.String(32), nullable=False, server_default=""),
    sa.Column("source_ip", sa.String(64), nullable=False, server_default=""),
    sa.Column("action", sa.String(64), nullable=False),
    sa.Column("model", sa.String(255)),
    sa.Column("prompt_content", sa.Text),
    sa.Column("prompt_hash", sa.String(128)),
    sa.Column("response_content", sa.Text),
    sa.Column("response_hash", sa.String(128)),
    sa.Column("token_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
    sa.Column("status", sa.String(32), nullable=False, server_default="success"),
    sa.Column("error_detail", sa.Text),
    sa.Column("chain_hash", sa.String(128)),
)

usage_table = sa.Table(
    "usage",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("hour", sa.String(32), nullable=False),
    sa.Column("user_id", sa.String(255), nullable=False),
    sa.Column("model", sa.String(255), nullable=False),
    sa.Column("request_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("total_latency_ms", sa.Integer, nullable=False, server_default="0"),
    sa.UniqueConstraint("hour", "user_id", "model", name="uq_usage_hour_user_model"),
)

adapters_table = sa.Table(
    "adapters",
    metadata,
    sa.Column("name", sa.String(255), primary_key=True),
    sa.Column("base_model", sa.String(255), nullable=False),
    sa.Column("source_path", sa.String(512), nullable=False),
    sa.Column(
        "state", sa.String(32), nullable=False, server_default="available"
    ),  # available, deployed, error
    sa.Column("training_job_id", sa.String(64)),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("deployed_at", sa.String(64)),
    sa.Column("metadata_json", sa.Text),
)

training_jobs_table = sa.Table(
    "training_jobs",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("base_model", sa.String(255), nullable=False),
    sa.Column("dataset", sa.String(255), nullable=False),
    sa.Column("config_json", sa.Text, nullable=False),
    sa.Column("state", sa.String(32), nullable=False, server_default="queued"),
    sa.Column("gpu", sa.Text),  # JSON array of GPU indices
    sa.Column("output_path", sa.String(512)),
    sa.Column("adapter_name", sa.String(255)),
    sa.Column("submitted_by", sa.String(255), nullable=False),
    sa.Column("submitted_at", sa.String(64), nullable=False),
    sa.Column("started_at", sa.String(64)),
    sa.Column("completed_at", sa.String(64)),
    sa.Column("error_message", sa.Text),
    sa.Column("metrics_json", sa.Text),
)

datasets_table = sa.Table(
    "datasets",
    metadata,
    sa.Column("name", sa.String(255), primary_key=True),
    sa.Column("path", sa.String(512), nullable=False),
    sa.Column("format", sa.String(32), nullable=False),  # sharegpt, alpaca, jsonl, csv
    sa.Column("row_count", sa.Integer),
    sa.Column("size_bytes", sa.Integer),
    sa.Column("uploaded_by", sa.String(255), nullable=False),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("metadata_json", sa.Text),
)

revoked_tokens_table = sa.Table(
    "revoked_tokens",
    metadata,
    sa.Column("jti", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(255), nullable=False),
    sa.Column("revoked_at", sa.String(64), nullable=False),
    sa.Column("expires_at", sa.String(64), nullable=False),
)

# Indexes for common queries
sa.Index("idx_audit_timestamp", audit_log_table.c.timestamp)
sa.Index("idx_audit_user", audit_log_table.c.user_id)
sa.Index("idx_audit_action", audit_log_table.c.action)
sa.Index("idx_usage_hour", usage_table.c.hour)
sa.Index("idx_training_state", training_jobs_table.c.state)
sa.Index("idx_revoked_expires", revoked_tokens_table.c.expires_at)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Pattern to match positional ? placeholders (not inside quoted strings)
_POSITIONAL_RE = re.compile(r"\?")


def _positional_to_named(sql: str, params: tuple | list) -> tuple[str, dict]:
    """Convert ``?``-style positional params to ``:p0, :p1`` named params.

    This bridge lets existing raw-SQL callers work unchanged with SQLAlchemy's
    ``text()`` which requires named parameters.
    """
    param_dict: dict[str, Any] = {}
    counter = 0

    def _replace(_match: re.Match) -> str:
        nonlocal counter
        name = f"p{counter}"
        param_dict[name] = params[counter]
        counter += 1
        return f":{name}"

    named_sql = _POSITIONAL_RE.sub(_replace, sql)
    return named_sql, param_dict


def get_database_url(settings: Any) -> str:
    """Build database URL from settings, defaulting to SQLite."""
    if settings.database.url:
        return settings.database.url
    cache_dir = Path(settings.cache.directory)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{cache_dir}/lean_ai_serve.db"


# ---------------------------------------------------------------------------
# Row wrapper for backwards compatibility
# ---------------------------------------------------------------------------


class _RowProxy:
    """Wraps a SQLAlchemy ``Row`` to support ``row['col']`` dict-style access."""

    __slots__ = ("_mapping",)

    def __init__(self, row: sa.Row):
        self._mapping = row._mapping

    def __getitem__(self, key: str | int) -> Any:
        return self._mapping[key]

    def __contains__(self, key: str) -> bool:
        return key in self._mapping

    def __iter__(self):
        return iter(self._mapping)

    def keys(self):
        return self._mapping.keys()

    def values(self):
        return self._mapping.values()

    def items(self):
        return self._mapping.items()


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------


class Database:
    """Async database wrapper using SQLAlchemy Core.

    Accepts a SQLAlchemy async URL (e.g. ``sqlite+aiosqlite:///path/db.sqlite``,
    ``postgresql+asyncpg://user:pass@host/db``, ``oracle+oracledb://...``).
    """

    def __init__(self, url: str | Path):
        # Accept a file path for backwards compatibility (converts to SQLite URL)
        if isinstance(url, Path):
            url = f"sqlite+aiosqlite:///{url}"
        self._url = url
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def dialect(self) -> str:
        """Return the dialect name (e.g. 'sqlite', 'postgresql', 'oracle')."""
        if self._engine is not None:
            return self._engine.dialect.name
        # Parse from URL before engine is created
        return self._url.split("+")[0].split("://")[0]

    async def connect(self) -> None:
        """Open the database and initialize schema."""
        is_sqlite = self._url.startswith("sqlite")

        engine_kwargs: dict[str, Any] = {}
        if not is_sqlite:
            engine_kwargs["pool_size"] = 5
            engine_kwargs["max_overflow"] = 10

        self._engine = create_async_engine(self._url, **engine_kwargs)

        # Ensure parent directory exists for SQLite
        if is_sqlite:
            db_path = self._url.split("///", 1)[-1] if "///" in self._url else ""
            if db_path:
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Create all tables (idempotent — uses IF NOT EXISTS)
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        self._conn = await self._engine.connect()
        logger.info("Database connected (%s)", self.dialect)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
        if self._engine:
            await self._engine.dispose()
            self._engine = None

    @property
    def engine(self) -> AsyncEngine:
        """Return the active engine (raises if not connected)."""
        if self._engine is None:
            raise RuntimeError("Database not connected — call connect() first")
        return self._engine

    @property
    def conn(self) -> AsyncConnection:
        """Return the active connection (raises if not connected)."""
        if self._conn is None:
            raise RuntimeError("Database not connected — call connect() first")
        return self._conn

    def _prepare(
        self, sql: str, params: tuple | list | dict | None
    ) -> tuple[sa.TextClause, dict]:
        """Convert raw SQL + params into a SQLAlchemy text() clause."""
        if isinstance(params, (tuple, list)) and params:
            named_sql, param_dict = _positional_to_named(sql, params)
            return sa.text(named_sql), param_dict
        if isinstance(params, dict):
            return sa.text(sql), params
        return sa.text(sql), {}

    async def execute(self, sql: str, params: tuple | list | dict | None = None) -> Any:
        """Execute a single SQL statement."""
        clause, param_dict = self._prepare(sql, params)
        return await self.conn.execute(clause, param_dict)

    async def executemany(
        self, sql: str, params_seq: list[tuple | dict]
    ) -> None:
        """Execute a SQL statement for each parameter set."""
        if not params_seq:
            return
        # Convert positional to named if needed
        if isinstance(params_seq[0], (tuple, list)):
            # Determine named SQL from first param set
            named_sql, _ = _positional_to_named(sql, params_seq[0])
            clause = sa.text(named_sql)
            dicts = []
            for params in params_seq:
                _, d = _positional_to_named(sql, params)
                dicts.append(d)
            await self.conn.execute(clause, dicts)
        else:
            await self.conn.execute(sa.text(sql), params_seq)

    async def fetchone(
        self, sql: str, params: tuple | list | dict | None = None
    ) -> _RowProxy | None:
        """Execute and fetch one row."""
        result = await self.execute(sql, params)
        row = result.first()
        return _RowProxy(row) if row is not None else None

    async def fetchall(
        self, sql: str, params: tuple | list | dict | None = None
    ) -> list[_RowProxy]:
        """Execute and fetch all rows."""
        result = await self.execute(sql, params)
        return [_RowProxy(row) for row in result.all()]

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self.conn.commit()

    async def upsert(
        self,
        table: sa.Table,
        values: dict[str, Any],
        *,
        conflict_columns: list[str] | None = None,
        update_columns: list[str] | None = None,
        on_conflict: str = "update",  # "update", "ignore", "replace"
    ) -> Any:
        """Dialect-aware upsert.

        Args:
            table: SQLAlchemy Table object.
            values: Column-value mapping to insert.
            conflict_columns: Columns that define the uniqueness constraint.
                Defaults to the table's primary key columns.
            update_columns: Columns to update on conflict.  Defaults to all
                non-conflict columns present in *values*.
            on_conflict: ``"update"`` (default), ``"ignore"``, or ``"replace"``.
        """
        dialect = self.dialect

        if conflict_columns is None:
            conflict_columns = [c.name for c in table.primary_key.columns]

        if update_columns is None and on_conflict == "update":
            update_columns = [k for k in values if k not in conflict_columns]

        if dialect in ("sqlite", "postgresql"):
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            _insert = pg_insert if dialect == "postgresql" else sqlite_insert
            stmt = _insert(table).values(**values)

            if on_conflict == "ignore":
                stmt = stmt.on_conflict_do_nothing(index_elements=conflict_columns)
            elif on_conflict == "replace":
                # Full replace: update ALL value columns
                update_cols = {k: v for k, v in values.items() if k not in conflict_columns}
                stmt = stmt.on_conflict_do_update(
                    index_elements=conflict_columns,
                    set_=update_cols,
                )
            else:  # "update"
                update_set = {col: stmt.excluded[col] for col in (update_columns or [])}
                stmt = stmt.on_conflict_do_update(
                    index_elements=conflict_columns,
                    set_=update_set,
                )
            return await self.conn.execute(stmt)

        elif dialect == "mysql":
            from sqlalchemy.dialects.mysql import insert as mysql_insert

            stmt = mysql_insert(table).values(**values)
            if on_conflict == "ignore":
                stmt = stmt.prefix_with("IGNORE")
            elif update_columns or on_conflict in ("update", "replace"):
                cols = update_columns or [
                    k for k in values if k not in conflict_columns
                ]
                update_set = {col: stmt.inserted[col] for col in cols}
                stmt = stmt.on_duplicate_key_update(**update_set)
            return await self.conn.execute(stmt)

        else:
            # Generic fallback (Oracle, etc.): SELECT then INSERT or UPDATE
            pk_filter = sa.and_(
                *(table.c[col] == values[col] for col in conflict_columns)
            )
            result = await self.conn.execute(
                sa.select(sa.literal(1)).select_from(table).where(pk_filter)
            )
            exists = result.first() is not None

            if exists:
                if on_conflict == "ignore":
                    return None
                cols = update_columns or [
                    k for k in values if k not in conflict_columns
                ]
                update_vals = {col: values[col] for col in cols if col in values}
                if update_vals:
                    return await self.conn.execute(
                        table.update().where(pk_filter).values(**update_vals)
                    )
                return None
            else:
                return await self.conn.execute(table.insert().values(**values))

    async def upsert_increment(
        self,
        table: sa.Table,
        values: dict[str, Any],
        *,
        conflict_columns: list[str],
        increment_columns: dict[str, Any],
    ) -> Any:
        """Upsert with increment-on-conflict semantics (for usage tracking).

        On conflict, increments the specified columns by the given amounts
        instead of replacing them.
        """
        dialect = self.dialect

        if dialect in ("sqlite", "postgresql"):
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            _insert = pg_insert if dialect == "postgresql" else sqlite_insert
            stmt = _insert(table).values(**values)
            update_set = {
                col: table.c[col] + stmt.excluded[col] for col in increment_columns
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=conflict_columns,
                set_=update_set,
            )
            return await self.conn.execute(stmt)

        elif dialect == "mysql":
            from sqlalchemy.dialects.mysql import insert as mysql_insert

            stmt = mysql_insert(table).values(**values)
            update_set = {
                col: table.c[col] + stmt.inserted[col] for col in increment_columns
            }
            stmt = stmt.on_duplicate_key_update(**update_set)
            return await self.conn.execute(stmt)

        else:
            # Generic fallback: SELECT then INSERT or UPDATE with increment
            pk_filter = sa.and_(
                *(table.c[col] == values[col] for col in conflict_columns)
            )
            result = await self.conn.execute(
                sa.select(sa.literal(1)).select_from(table).where(pk_filter)
            )
            if result.first() is not None:
                update_vals = {
                    col: table.c[col] + increment_columns[col]
                    for col in increment_columns
                }
                return await self.conn.execute(
                    table.update().where(pk_filter).values(**update_vals)
                )
            else:
                return await self.conn.execute(table.insert().values(**values))
