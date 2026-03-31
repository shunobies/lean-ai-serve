"""Tests for the sliding window rate limiter."""

from __future__ import annotations

import time

from lean_ai_serve.security.rate_limiter import RateLimiter


def test_allows_under_limit():
    """Requests under the limit should be allowed."""
    rl = RateLimiter()
    for _ in range(5):
        allowed, headers = rl.check("key1", 10)
        assert allowed is True
    assert int(headers["X-RateLimit-Limit"]) == 10
    assert int(headers["X-RateLimit-Remaining"]) >= 0


def test_blocks_over_limit():
    """Exceeding the rate limit should return 429-ready headers."""
    rl = RateLimiter()
    for _ in range(5):
        rl.check("key1", 5)

    allowed, headers = rl.check("key1", 5)
    assert allowed is False
    assert "Retry-After" in headers
    assert int(headers["X-RateLimit-Remaining"]) == 0


def test_key_isolation():
    """Different API keys should have independent windows."""
    rl = RateLimiter()
    for _ in range(5):
        rl.check("key1", 5)

    # key1 is exhausted
    allowed, _ = rl.check("key1", 5)
    assert allowed is False

    # key2 should still be allowed
    allowed, _ = rl.check("key2", 5)
    assert allowed is True


def test_unlimited_key():
    """A limit of 0 means unlimited — always allowed, no headers."""
    rl = RateLimiter()
    for _ in range(100):
        allowed, headers = rl.check("key1", 0)
        assert allowed is True
        assert headers == {}


def test_window_expiry():
    """Requests should become available again after the window expires."""
    rl = RateLimiter()
    for _ in range(3):
        rl.check("key1", 3, window_seconds=1)

    allowed, _ = rl.check("key1", 3, window_seconds=1)
    assert allowed is False

    # Simulate time passing by manipulating the deque
    window = rl._windows["key1"]
    past = time.monotonic() - 2
    for i in range(len(window)):
        window[i] = past

    allowed, _ = rl.check("key1", 3, window_seconds=1)
    assert allowed is True


def test_headers_present():
    """Rate limit headers should be present on allowed requests."""
    rl = RateLimiter()
    allowed, headers = rl.check("key1", 10)
    assert allowed is True
    assert "X-RateLimit-Limit" in headers
    assert "X-RateLimit-Remaining" in headers
    assert "X-RateLimit-Reset" in headers


def test_cleanup():
    """Cleanup should remove empty windows."""
    rl = RateLimiter()
    rl._windows["empty"] = __import__("collections").deque()
    rl._windows["notempty"] = __import__("collections").deque([time.monotonic()])

    cleaned = rl.cleanup()
    assert cleaned == 1
    assert "empty" not in rl._windows
    assert "notempty" in rl._windows
