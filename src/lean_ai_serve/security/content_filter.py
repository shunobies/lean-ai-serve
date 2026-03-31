"""PHI pattern detection middleware for content filtering."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from lean_ai_serve.config import ContentFilterConfig

logger = logging.getLogger(__name__)

# Paths that should be scanned for PHI
_INFERENCE_PATHS = {"/v1/chat/completions", "/v1/completions", "/v1/embeddings"}


@dataclass
class ContentFilterMatch:
    """A single match from content scanning."""

    name: str
    matched_text: str
    start: int
    end: int
    action: str  # warn, redact, block


class ContentFilter:
    """Scans text for PHI patterns and applies configured actions."""

    def __init__(self, config: ContentFilterConfig):
        self._patterns: list[tuple[str, re.Pattern, str]] = []

        for p in config.patterns:
            try:
                compiled = re.compile(p.pattern)
                self._patterns.append((p.name, compiled, p.action))
            except re.error as e:
                logger.error("Invalid content filter pattern '%s': %s", p.name, e)

        # Load custom patterns file if specified
        if config.custom_patterns_file:
            self._load_custom_patterns(config.custom_patterns_file)

        logger.info("Content filter initialized with %d patterns", len(self._patterns))

    def _load_custom_patterns(self, path: str) -> None:
        """Load additional patterns from a YAML file."""
        try:
            import yaml

            with open(path) as f:
                data = yaml.safe_load(f) or []
            for item in data:
                compiled = re.compile(item["pattern"])
                self._patterns.append((item["name"], compiled, item.get("action", "warn")))
        except Exception as e:
            logger.error("Failed to load custom patterns from '%s': %s", path, e)

    def scan(self, text: str) -> list[ContentFilterMatch]:
        """Scan text against all patterns. Returns list of matches."""
        matches = []
        for name, pattern, action in self._patterns:
            for m in pattern.finditer(text):
                matches.append(
                    ContentFilterMatch(
                        name=name,
                        matched_text=m.group(),
                        start=m.start(),
                        end=m.end(),
                        action=action,
                    )
                )
        return matches

    def redact(self, text: str, matches: list[ContentFilterMatch]) -> str:
        """Replace matched text with [REDACTED:{name}] markers.

        Processes matches from end to start to preserve positions.
        """
        sorted_matches = sorted(matches, key=lambda m: m.start, reverse=True)
        result = text
        for m in sorted_matches:
            if m.action == "redact":
                result = result[: m.start] + f"[REDACTED:{m.name}]" + result[m.end :]
        return result


class ContentFilterMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that scans inference request bodies for PHI patterns."""

    def __init__(self, app, content_filter: ContentFilter):
        super().__init__(app)
        self._filter = content_filter

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Only filter inference endpoints
        if request.url.path not in _INFERENCE_PATHS:
            return await call_next(request)

        if request.method != "POST":
            return await call_next(request)

        # Read and scan request body
        body = await request.body()
        if not body:
            return await call_next(request)

        try:
            text = body.decode()
        except UnicodeDecodeError:
            return await call_next(request)

        matches = self._filter.scan(text)
        if not matches:
            return await call_next(request)

        # Check for blocking matches
        blocking = [m for m in matches if m.action == "block"]
        if blocking:
            pattern_names = ", ".join(m.name for m in blocking)
            logger.warning(
                "Content filter BLOCKED request to %s: patterns=%s",
                request.url.path,
                pattern_names,
            )
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "message": f"Request blocked: sensitive content detected ({pattern_names})",
                        "type": "content_filter",
                    }
                },
            )

        # Log warnings
        for m in matches:
            if m.action == "warn":
                logger.warning(
                    "Content filter WARNING: pattern '%s' detected in request to %s",
                    m.name,
                    request.url.path,
                )

        # Apply redactions if any
        redact_matches = [m for m in matches if m.action == "redact"]
        if redact_matches:
            redacted = self._filter.redact(text, redact_matches)
            # Modify the request body
            # We need to create a new request scope with the modified body
            request._body = redacted.encode()

        return await call_next(request)
