"""Configurable alert thresholds with Prometheus ALERTS integration."""

from __future__ import annotations

import logging
import time
from enum import StrEnum

from pydantic import BaseModel

from lean_ai_serve.observability.metrics import MetricsCollector

logger = logging.getLogger(__name__)


class AlertSeverity(StrEnum):
    info = "info"
    warning = "warning"
    critical = "critical"


class AlertState(StrEnum):
    ok = "ok"
    firing = "firing"
    resolved = "resolved"


class AlertRule(BaseModel):
    """A single alert rule definition."""

    name: str
    metric: str  # Gauge metric to check (e.g., "gpu_memory_used_pct")
    condition: str = "gt"  # gt, lt, gte, lte, eq
    threshold: float = 0.0
    severity: str = "warning"
    message: str = ""
    labels: dict[str, str] | None = None  # Optional label filter for the metric


class AlertEvent(BaseModel):
    """An alert evaluation result."""

    rule_name: str
    state: str  # AlertState value
    severity: str
    message: str
    value: float = 0.0
    fired_at: float = 0.0  # monotonic timestamp


# Condition evaluators
_CONDITIONS = {
    "gt": lambda v, t: v > t,
    "lt": lambda v, t: v < t,
    "gte": lambda v, t: v >= t,
    "lte": lambda v, t: v <= t,
    "eq": lambda v, t: v == t,
}

# Default alert rules (applied when no custom rules are configured)
DEFAULT_RULES = [
    AlertRule(
        name="high_gpu_memory",
        metric="gpu_memory_used_pct",
        condition="gt",
        threshold=90.0,
        severity="warning",
        message="GPU memory usage above 90%",
    ),
    AlertRule(
        name="high_error_rate",
        metric="error_rate_pct",
        condition="gt",
        threshold=5.0,
        severity="critical",
        message="Error rate exceeds 5%",
    ),
]


class AlertEvaluator:
    """Evaluates alert rules against current metric values."""

    def __init__(
        self, metrics: MetricsCollector, rules: list[AlertRule] | None = None
    ) -> None:
        self._metrics = metrics
        self._rules = rules if rules is not None else list(DEFAULT_RULES)
        self._active: dict[str, AlertEvent] = {}  # rule_name -> AlertEvent

    def evaluate(self) -> list[AlertEvent]:
        """Evaluate all rules and return alert events with state changes."""
        events: list[AlertEvent] = []
        for rule in self._rules:
            value = self._get_metric_value(rule)
            check = _CONDITIONS.get(rule.condition, _CONDITIONS["gt"])
            is_firing = check(value, rule.threshold)

            prev = self._active.get(rule.name)
            if is_firing and (prev is None or prev.state != AlertState.firing):
                # New firing alert
                event = AlertEvent(
                    rule_name=rule.name,
                    state=AlertState.firing,
                    severity=rule.severity,
                    message=rule.message or f"{rule.metric} {rule.condition} {rule.threshold}",
                    value=value,
                    fired_at=time.monotonic(),
                )
                self._active[rule.name] = event
                events.append(event)
                logger.warning(
                    "Alert FIRING: %s (value=%.2f, threshold=%.2f)",
                    rule.name, value, rule.threshold,
                )
            elif not is_firing and prev is not None and prev.state == AlertState.firing:
                # Alert resolved
                event = AlertEvent(
                    rule_name=rule.name,
                    state=AlertState.resolved,
                    severity=rule.severity,
                    message=f"Resolved: {rule.message}",
                    value=value,
                )
                self._active[rule.name] = event
                events.append(event)
                logger.info("Alert RESOLVED: %s (value=%.2f)", rule.name, value)

        return events

    def get_active_alerts(self) -> list[AlertEvent]:
        """Return currently firing alerts."""
        return [a for a in self._active.values() if a.state == AlertState.firing]

    def expose_alerts(self) -> str:
        """Render active alerts in Prometheus ALERTS format."""
        lines = [
            "# HELP ALERTS Active alerts",
            "# TYPE ALERTS gauge",
        ]
        for alert in self.get_active_alerts():
            lines.append(
                f'ALERTS{{alertname="{alert.rule_name}",'
                f'severity="{alert.severity}"}} 1'
            )
        return "\n".join(lines)

    def _get_metric_value(self, rule: AlertRule) -> float:
        """Read the current value of a metric referenced by the rule."""
        labels = rule.labels or {}

        # Map rule metric names to collector attributes
        metric_map = {
            "gpu_memory_used_pct": self._metrics.gpu_utilization_pct,
            "gpu_memory_used_bytes": self._metrics.gpu_memory_used_bytes,
            "models_loaded": self._metrics.models_loaded,
            "training_jobs_active": self._metrics.training_jobs_active,
        }

        gauge = metric_map.get(rule.metric)
        if gauge is not None:
            return gauge.get(**labels)

        # Special computed metrics
        if rule.metric == "error_rate_pct":
            return self._compute_error_rate()

        return 0.0

    def _compute_error_rate(self) -> float:
        """Compute error rate as percentage of 5xx responses."""
        total = sum(self._metrics.requests_total._values.values())
        if total == 0:
            return 0.0
        errors = sum(
            v for k, v in self._metrics.requests_total._values.items()
            if any(kk == "status" and vv.startswith("5") for kk, vv in k)
        )
        return (errors / total) * 100
