"""Per-API-key sliding window rate limiter."""

from __future__ import annotations

import time
from collections import deque

from fastapi import Depends, HTTPException, Request

from lean_ai_serve.models.schemas import AuthUser
from lean_ai_serve.security.auth import authenticate

WINDOW_SECONDS = 60  # 1-minute sliding window


class RateLimiter:
    """In-memory sliding window rate limiter.

    Tracks request timestamps per API key. Single-process safe (no locking needed
    since FastAPI runs on a single asyncio event loop).
    """

    def __init__(self) -> None:
        self._windows: dict[str, deque[float]] = {}

    def check(
        self, key_id: str, limit: int, window_seconds: int = WINDOW_SECONDS
    ) -> tuple[bool, dict[str, str]]:
        """Check if a request is within rate limits.

        Returns (allowed, headers) where headers contain X-RateLimit-* values.
        """
        if limit <= 0:
            return True, {}

        now = time.monotonic()
        window = self._windows.setdefault(key_id, deque())

        # Purge timestamps outside the window
        cutoff = now - window_seconds
        while window and window[0] < cutoff:
            window.popleft()

        remaining = max(0, limit - len(window))
        reset_at = int(window[0] + window_seconds) if window else int(now + window_seconds)

        headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_at),
        }

        if len(window) >= limit:
            retry_after = int(window[0] + window_seconds - now) + 1
            headers["Retry-After"] = str(retry_after)
            return False, headers

        window.append(now)
        # Update remaining after adding
        headers["X-RateLimit-Remaining"] = str(remaining - 1 if remaining > 0 else 0)
        return True, headers

    def cleanup(self) -> int:
        """Remove empty windows. Returns number cleaned."""
        empty = [k for k, v in self._windows.items() if not v]
        for k in empty:
            del self._windows[k]
        return len(empty)


# Module-level singleton
rate_limiter = RateLimiter()


async def check_rate_limit(
    request: Request,
    user: AuthUser = Depends(authenticate),
) -> AuthUser:
    """FastAPI dependency that enforces rate limits for API key users."""
    if user.auth_method == "api_key" and user.rate_limit > 0:
        allowed, headers = rate_limiter.check(user.key_id, user.rate_limit)
        # Always set rate limit headers on the response
        request.state.rate_limit_headers = headers
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers=headers,
            )
    return user
