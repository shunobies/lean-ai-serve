"""Usage tracking — aggregates token counts and latency into hourly buckets."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from lean_ai_serve.db import Database

logger = logging.getLogger(__name__)


class UsageTracker:
    """Records and queries inference usage data.

    Uses the existing ``usage`` table with UNIQUE(hour, user_id, model).
    Each call to :meth:`record` atomically increments the counters for the
    current hourly bucket via SQL UPSERT.
    """

    def __init__(self, db: Database):
        self._db = db

    @staticmethod
    def _current_hour() -> str:
        """Return the current hour as an ISO 8601 string truncated to the hour."""
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:00:00")

    async def record(
        self,
        *,
        user_id: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
    ) -> None:
        """Record a single request's usage into the hourly bucket."""
        hour = self._current_hour()
        await self._db.execute(
            """
            INSERT INTO usage (hour, user_id, model,
                               request_count, prompt_tokens, completion_tokens,
                               total_latency_ms)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(hour, user_id, model) DO UPDATE SET
                request_count = request_count + 1,
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                total_latency_ms = total_latency_ms + excluded.total_latency_ms
            """,
            (hour, user_id, model, prompt_tokens, completion_tokens, latency_ms),
        )
        await self._db.commit()

    async def query(
        self,
        *,
        user_id: str | None = None,
        model: str | None = None,
        from_hour: str | None = None,
        to_hour: str | None = None,
        limit: int = 168,
    ) -> list[dict]:
        """Query usage records with optional filters."""
        conditions: list[str] = []
        params: list = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if model:
            conditions.append("model = ?")
            params.append(model)
        if from_hour:
            conditions.append("hour >= ?")
            params.append(from_hour)
        if to_hour:
            conditions.append("hour <= ?")
            params.append(to_hour)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = await self._db.fetchall(
            f"""
            SELECT hour, user_id, model,
                   request_count, prompt_tokens, completion_tokens, total_latency_ms
            FROM usage
            WHERE {where}
            ORDER BY hour DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        return [dict(row) for row in rows]

    async def get_user_summary(
        self, user_id: str, period_hours: int = 24
    ) -> dict:
        """Aggregate usage for a user over the given period."""
        cutoff = (datetime.now(UTC) - timedelta(hours=period_hours)).strftime(
            "%Y-%m-%dT%H:00:00"
        )
        row = await self._db.fetchone(
            """
            SELECT COALESCE(SUM(request_count), 0) AS request_count,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_latency_ms), 0) AS total_latency_ms
            FROM usage
            WHERE user_id = ? AND hour >= ?
            """,
            (user_id, cutoff),
        )
        models_rows = await self._db.fetchall(
            "SELECT DISTINCT model FROM usage WHERE user_id = ? AND hour >= ?",
            (user_id, cutoff),
        )
        return {
            "user_id": user_id,
            "period_hours": period_hours,
            "request_count": row["request_count"],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "total_latency_ms": row["total_latency_ms"],
            "models_used": [r["model"] for r in models_rows],
        }

    async def get_model_summary(
        self, model: str, period_hours: int = 24
    ) -> dict:
        """Aggregate usage for a model over the given period."""
        cutoff = (datetime.now(UTC) - timedelta(hours=period_hours)).strftime(
            "%Y-%m-%dT%H:00:00"
        )
        row = await self._db.fetchone(
            """
            SELECT COALESCE(SUM(request_count), 0) AS request_count,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_latency_ms), 0) AS total_latency_ms,
                   COUNT(DISTINCT user_id) AS unique_users
            FROM usage
            WHERE model = ? AND hour >= ?
            """,
            (model, cutoff),
        )
        return {
            "model": model,
            "period_hours": period_hours,
            "request_count": row["request_count"],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "total_latency_ms": row["total_latency_ms"],
            "unique_users": row["unique_users"],
        }

    async def cleanup(self, retention_days: int = 90) -> int:
        """Delete usage records older than *retention_days*. Returns count deleted."""
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime(
            "%Y-%m-%dT%H:00:00"
        )
        result = await self._db.execute(
            "DELETE FROM usage WHERE hour < ?", (cutoff,)
        )
        await self._db.commit()
        deleted = result.rowcount
        if deleted:
            logger.info("Cleaned up %d usage records older than %s", deleted, cutoff)
        return deleted
