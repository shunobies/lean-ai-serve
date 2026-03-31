"""Tests for alert threshold evaluator."""

from __future__ import annotations

from lean_ai_serve.observability.alerts import (
    DEFAULT_RULES,
    AlertEvaluator,
    AlertRule,
    AlertState,
)
from lean_ai_serve.observability.metrics import MetricsCollector


def _make_evaluator(rules=None):
    mc = MetricsCollector()
    ev = AlertEvaluator(mc, rules=rules)
    return mc, ev


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------


def test_alert_fires_on_threshold_breach():
    """Rule fires when metric exceeds threshold."""
    rule = AlertRule(
        name="high_gpu", metric="gpu_memory_used_pct", condition="gt",
        threshold=80.0, labels={"gpu": "0"},
    )
    mc, ev = _make_evaluator([rule])
    mc.gpu_utilization_pct.set(95.0, gpu="0")
    events = ev.evaluate()
    assert len(events) == 1
    assert events[0].state == AlertState.firing
    assert events[0].rule_name == "high_gpu"


def test_alert_stays_ok_below_threshold():
    """Rule stays ok when metric is below threshold."""
    rule = AlertRule(
        name="high_gpu", metric="gpu_memory_used_pct", condition="gt",
        threshold=80.0, labels={"gpu": "0"},
    )
    mc, ev = _make_evaluator([rule])
    mc.gpu_utilization_pct.set(50.0, gpu="0")
    events = ev.evaluate()
    assert len(events) == 0
    assert len(ev.get_active_alerts()) == 0


def test_alert_state_transitions():
    """Alert transitions: ok -> firing -> resolved."""
    rule = AlertRule(name="test", metric="models_loaded", condition="lt", threshold=1.0)
    mc, ev = _make_evaluator([rule])

    # Trigger: no models loaded (0 < 1)
    events = ev.evaluate()
    assert len(events) == 1
    assert events[0].state == AlertState.firing

    # Still firing (no state change emitted)
    events = ev.evaluate()
    assert len(events) == 0

    # Resolve: load a model (1 is not < 1)
    mc.models_loaded.set(1.0)
    events = ev.evaluate()
    assert len(events) == 1
    assert events[0].state == AlertState.resolved


def test_get_active_alerts():
    """get_active_alerts returns only firing alerts."""
    rules = [
        AlertRule(name="r1", metric="models_loaded", condition="lt", threshold=1.0),
        AlertRule(name="r2", metric="models_loaded", condition="gt", threshold=10.0),
    ]
    mc, ev = _make_evaluator(rules)
    # r1 fires (0 < 1), r2 doesn't (0 is not > 10)
    ev.evaluate()
    active = ev.get_active_alerts()
    assert len(active) == 1
    assert active[0].rule_name == "r1"


# ---------------------------------------------------------------------------
# Expose format
# ---------------------------------------------------------------------------


def test_expose_alerts_format():
    """expose_alerts renders Prometheus ALERTS format."""
    rule = AlertRule(
        name="high_gpu", metric="models_loaded", condition="lt", threshold=1.0,
        severity="critical",
    )
    mc, ev = _make_evaluator([rule])
    ev.evaluate()
    text = ev.expose_alerts()
    assert "# HELP ALERTS Active alerts" in text
    assert 'alertname="high_gpu"' in text
    assert 'severity="critical"' in text


# ---------------------------------------------------------------------------
# Default and custom rules
# ---------------------------------------------------------------------------


def test_default_rules_included():
    """Default rules are used when no custom rules provided."""
    mc = MetricsCollector()
    ev = AlertEvaluator(mc)
    assert len(ev._rules) == len(DEFAULT_RULES)


def test_custom_rules_override_defaults():
    """Custom rules replace defaults."""
    rule = AlertRule(name="custom", metric="models_loaded", condition="eq", threshold=0.0)
    mc, ev = _make_evaluator([rule])
    assert len(ev._rules) == 1
    assert ev._rules[0].name == "custom"


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def test_condition_lt():
    """Less-than condition."""
    rule = AlertRule(name="test", metric="models_loaded", condition="lt", threshold=5.0)
    mc, ev = _make_evaluator([rule])
    mc.models_loaded.set(3.0)
    events = ev.evaluate()
    assert len(events) == 1


def test_condition_gte():
    """Greater-than-or-equal condition."""
    rule = AlertRule(name="test", metric="models_loaded", condition="gte", threshold=3.0)
    mc, ev = _make_evaluator([rule])
    mc.models_loaded.set(3.0)
    events = ev.evaluate()
    assert len(events) == 1


def test_error_rate_computation():
    """Error rate computed from request status codes."""
    rule = AlertRule(name="errors", metric="error_rate_pct", condition="gt", threshold=5.0)
    mc, ev = _make_evaluator([rule])
    # 10 total requests, 1 error = 10%
    for _ in range(9):
        mc.requests_total.inc(method="GET", path="/api", status="200")
    mc.requests_total.inc(method="GET", path="/api", status="500")
    events = ev.evaluate()
    assert len(events) == 1
    assert events[0].value == 10.0
