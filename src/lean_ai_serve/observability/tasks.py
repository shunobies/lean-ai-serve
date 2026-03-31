"""Background maintenance tasks — cleanup, GPU polling, alert evaluation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from lean_ai_serve.config import Settings
from lean_ai_serve.db import Database

if TYPE_CHECKING:
    from lean_ai_serve.observability.alerts import AlertEvaluator
    from lean_ai_serve.observability.metrics import MetricsCollector
    from lean_ai_serve.security.rate_limiter import RateLimiter
    from lean_ai_serve.security.usage import UsageTracker

logger = logging.getLogger(__name__)


class BackgroundScheduler:
    """Manages periodic background tasks with independent intervals.

    Each task runs in its own asyncio Task. Errors are logged but
    don't crash the scheduler loop.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        metrics: MetricsCollector | None = None,
        rate_limiter: RateLimiter | None = None,
        usage_tracker: UsageTracker | None = None,
        alert_evaluator: AlertEvaluator | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._metrics = metrics
        self._rate_limiter = rate_limiter
        self._usage_tracker = usage_tracker
        self._alert_evaluator = alert_evaluator
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Launch all background tasks."""
        self._tasks = [
            asyncio.create_task(
                self._run_periodic("token_cleanup", 3600, self._cleanup_tokens)
            ),
            asyncio.create_task(
                self._run_periodic("rate_limiter_cleanup", 300, self._cleanup_rate_limiter)
            ),
            asyncio.create_task(
                self._run_periodic("audit_retention", 86400, self._cleanup_audit)
            ),
            asyncio.create_task(
                self._run_periodic("usage_retention", 86400, self._cleanup_usage)
            ),
            asyncio.create_task(
                self._run_periodic(
                    "gpu_snapshot",
                    self._settings.metrics.gpu_poll_interval,
                    self._snapshot_gpu,
                )
            ),
            asyncio.create_task(
                self._run_periodic("zombie_reaper", 300, self._reap_zombies)
            ),
            asyncio.create_task(
                self._run_periodic(
                    "alert_evaluation",
                    self._settings.alerts.evaluation_interval,
                    self._evaluate_alerts,
                )
            ),
        ]
        logger.info("Background scheduler started (%d tasks)", len(self._tasks))

    async def stop(self) -> None:
        """Cancel all background tasks."""
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        logger.info("Background scheduler stopped")

    async def _run_periodic(self, name: str, interval: int | float, func) -> None:
        """Run func every interval seconds, logging errors."""
        while True:
            try:
                await asyncio.sleep(interval)
                await func()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background task '%s' failed", name)

    # ----- Task implementations -----

    async def _cleanup_tokens(self) -> None:
        """Remove expired revoked JWT tokens."""
        from lean_ai_serve.security.auth import cleanup_revoked_tokens

        count = await cleanup_revoked_tokens(self._db)
        if count:
            logger.info("Cleaned up %d expired revoked tokens", count)

    async def _cleanup_rate_limiter(self) -> None:
        """Prune empty rate limiter sliding windows."""
        if self._rate_limiter:
            count = self._rate_limiter.cleanup()
            if count:
                logger.debug("Cleaned up %d empty rate limiter windows", count)

    async def _cleanup_audit(self) -> None:
        """Delete audit entries past retention period."""
        retention_days = self._settings.audit.retention_days
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        result = await self._db.execute(
            "DELETE FROM audit_log WHERE timestamp < ?", (cutoff,)
        )
        await self._db.commit()
        if result.rowcount:
            logger.info(
                "Audit retention: deleted %d entries older than %d days",
                result.rowcount,
                retention_days,
            )

    async def _cleanup_usage(self) -> None:
        """Delete old usage records."""
        if self._usage_tracker:
            await self._usage_tracker.cleanup()

    async def _snapshot_gpu(self) -> None:
        """Poll GPU info and update metrics gauges."""
        if self._metrics is None:
            return
        from lean_ai_serve.utils.gpu import get_gpu_info

        gpus = get_gpu_info()
        self._metrics.record_gpu_snapshot(gpus)

    async def _reap_zombies(self) -> None:
        """Detect training jobs stuck in RUNNING state with dead processes."""
        try:
            import psutil
        except ImportError:
            return

        rows = await self._db.fetchall(
            "SELECT id, pid FROM training_jobs WHERE state = 'running'"
        )
        if not rows:
            return

        for row in rows:
            pid = row.get("pid")
            if pid and not psutil.pid_exists(pid):
                await self._db.execute(
                    "UPDATE training_jobs SET state = 'failed', "
                    "error = 'Process died unexpectedly' WHERE id = ?",
                    (row["id"],),
                )
                await self._db.commit()
                logger.warning("Reaped zombie training job %s (pid=%d)", row["id"], pid)

    async def _evaluate_alerts(self) -> None:
        """Evaluate alert rules against current metrics."""
        if self._alert_evaluator:
            self._alert_evaluator.evaluate()
