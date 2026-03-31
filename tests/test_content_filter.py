"""Tests for PHI content filtering."""

from __future__ import annotations

import pytest

from lean_ai_serve.config import ContentFilterConfig, ContentFilterPattern
from lean_ai_serve.security.content_filter import ContentFilter


@pytest.fixture
def ssn_config() -> ContentFilterConfig:
    """Config with SSN detection pattern."""
    return ContentFilterConfig(
        enabled=True,
        patterns=[
            ContentFilterPattern(
                name="SSN",
                pattern=r"\b\d{3}-\d{2}-\d{4}\b",
                action="block",
            ),
        ],
    )


@pytest.fixture
def multi_config() -> ContentFilterConfig:
    """Config with multiple patterns and actions."""
    return ContentFilterConfig(
        enabled=True,
        patterns=[
            ContentFilterPattern(
                name="SSN",
                pattern=r"\b\d{3}-\d{2}-\d{4}\b",
                action="block",
            ),
            ContentFilterPattern(
                name="MRN",
                pattern=r"\bMRN\s*[:# ]\s*\d{6,10}\b",
                action="redact",
            ),
            ContentFilterPattern(
                name="email",
                pattern=r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
                action="warn",
            ),
        ],
    )


def test_ssn_detection(ssn_config):
    """Should detect SSN patterns."""
    cf = ContentFilter(ssn_config)
    matches = cf.scan("Patient SSN is 123-45-6789")
    assert len(matches) == 1
    assert matches[0].name == "SSN"
    assert matches[0].matched_text == "123-45-6789"
    assert matches[0].action == "block"


def test_no_match(ssn_config):
    """Should return empty list when no patterns match."""
    cf = ContentFilter(ssn_config)
    matches = cf.scan("No sensitive data here")
    assert matches == []


def test_multiple_matches(multi_config):
    """Should detect all matching patterns."""
    cf = ContentFilter(multi_config)
    text = "Patient SSN: 123-45-6789, MRN: 1234567, email: john@example.com"
    matches = cf.scan(text)
    names = {m.name for m in matches}
    assert names == {"SSN", "MRN", "email"}


def test_redaction(multi_config):
    """Should redact matched text for 'redact' action only."""
    cf = ContentFilter(multi_config)
    text = "Patient MRN: 1234567 has SSN 123-45-6789"
    matches = cf.scan(text)
    redacted = cf.redact(text, matches)
    # MRN should be redacted
    assert "[REDACTED:MRN]" in redacted
    # SSN action is 'block', not 'redact', so it should remain
    assert "123-45-6789" in redacted


def test_redaction_preserves_non_matches(multi_config):
    """Redaction should only touch matched portions."""
    cf = ContentFilter(multi_config)
    text = "The MRN: 1234567 belongs to patient John"
    matches = cf.scan(text)
    redacted = cf.redact(text, matches)
    assert redacted.startswith("The ")
    assert redacted.endswith(" belongs to patient John")


def test_multiple_redactions():
    """Should handle multiple redactions in same text."""
    config = ContentFilterConfig(
        enabled=True,
        patterns=[
            ContentFilterPattern(
                name="phone",
                pattern=r"\b\d{3}-\d{3}-\d{4}\b",
                action="redact",
            ),
        ],
    )
    cf = ContentFilter(config)
    text = "Call 555-123-4567 or 555-987-6543"
    matches = cf.scan(text)
    assert len(matches) == 2
    redacted = cf.redact(text, matches)
    assert redacted.count("[REDACTED:phone]") == 2


def test_empty_patterns():
    """Empty pattern list should never match."""
    config = ContentFilterConfig(enabled=True, patterns=[])
    cf = ContentFilter(config)
    matches = cf.scan("anything 123-45-6789")
    assert matches == []


def test_invalid_regex_ignored():
    """Invalid regex should be skipped with a warning, not crash."""
    config = ContentFilterConfig(
        enabled=True,
        patterns=[
            ContentFilterPattern(
                name="bad",
                pattern=r"[invalid(",
                action="warn",
            ),
            ContentFilterPattern(
                name="good",
                pattern=r"\d+",
                action="warn",
            ),
        ],
    )
    cf = ContentFilter(config)
    assert len(cf._patterns) == 1  # Only the valid pattern
    matches = cf.scan("test 123")
    assert len(matches) == 1
    assert matches[0].name == "good"


def test_match_positions(ssn_config):
    """Match start/end positions should be correct."""
    cf = ContentFilter(ssn_config)
    text = "SSN: 123-45-6789 done"
    matches = cf.scan(text)
    assert matches[0].start == 5
    assert matches[0].end == 16
    assert text[matches[0].start : matches[0].end] == "123-45-6789"
