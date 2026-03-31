"""Context compression middleware — shrinks long prompts before proxying to vLLM.

Uses LLMlingua2 (optional dependency) to compress lengthy messages while
preserving semantic meaning.  Short prompts pass through unchanged.

Compression is applied to ``/v1/chat/completions`` and ``/v1/completions``
only.  Response headers indicate whether compression was applied and the
size reduction achieved.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from lean_ai_serve.config import ContextCompressionConfig

logger = logging.getLogger(__name__)

# Paths that support context compression
_COMPRESSIBLE_PATHS = {"/v1/chat/completions", "/v1/completions"}

# Preserve the N most recent messages uncompressed (chat completions)
_PRESERVE_RECENT = 2


class ContextCompressor:
    """Wraps LLMlingua2 for context-aware prompt compression.

    Lazy-loads the model on first call to avoid startup cost when compression
    is enabled but not yet triggered.
    """

    def __init__(self, config: ContextCompressionConfig):
        self._config = config
        self._compressor: Any = None
        self._initialized = False

    def _ensure_loaded(self) -> None:
        """Lazy-load the LLMlingua2 model."""
        if self._initialized:
            return
        try:
            from llmlingua import PromptCompressor

            self._compressor = PromptCompressor(
                model_name="microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank",
                use_llmlingua2=True,
                device_map="cpu",  # Compression on CPU — GPU reserved for inference
            )
            self._initialized = True
            logger.info(
                "LLMlingua2 compressor loaded (target_ratio=%.2f)",
                self._config.target_ratio,
            )
        except ImportError:
            logger.warning(
                "llmlingua not installed — context compression disabled. "
                "Install with: pip install lean-ai-serve[compression]"
            )
            self._initialized = True  # Don't retry

    def compress(self, text: str) -> str:
        """Compress a single text string.

        Returns the original text if it's below ``min_length`` or if the
        compressor is unavailable.
        """
        if len(text) < self._config.min_length:
            return text

        self._ensure_loaded()
        if self._compressor is None:
            return text

        try:
            result = self._compressor.compress_prompt(
                [text],
                rate=self._config.target_ratio,
                force_tokens=["\n", ".", "?", "!"],
            )
            compressed = result.get("compressed_prompt", text)
            logger.debug(
                "Compressed %d -> %d chars (%.1f%%)",
                len(text), len(compressed),
                (1 - len(compressed) / len(text)) * 100,
            )
            return compressed
        except Exception:
            logger.exception("Compression failed — using original text")
            return text

    def compress_messages(self, messages: list[dict]) -> tuple[list[dict], int, int]:
        """Compress eligible messages in a chat completions payload.

        Preserves the last ``_PRESERVE_RECENT`` messages uncompressed (recent
        context is most important).  Only compresses messages whose content
        exceeds ``min_length``.

        Returns (compressed_messages, original_total_len, compressed_total_len).
        """
        if not messages:
            return messages, 0, 0

        original_len = sum(len(m.get("content", "")) for m in messages)

        # Identify which messages can be compressed (all except last N)
        protect_start = max(0, len(messages) - _PRESERVE_RECENT)
        result = []
        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            if i < protect_start and isinstance(content, str):
                compressed_content = self.compress(content)
                result.append({**msg, "content": compressed_content})
            else:
                result.append(msg)

        compressed_len = sum(len(m.get("content", "")) for m in result)
        return result, original_len, compressed_len

    def compress_prompt(self, prompt: str) -> tuple[str, int, int]:
        """Compress a plain text prompt (completions endpoint).

        Returns (compressed_text, original_len, compressed_len).
        """
        original_len = len(prompt)
        compressed = self.compress(prompt)
        return compressed, original_len, len(compressed)


class CompressionMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that compresses long prompts before forwarding.

    Intercepts ``/v1/chat/completions`` and ``/v1/completions``, compresses
    eligible content, and adds response headers:

    - ``X-Context-Compressed: true|false``
    - ``X-Context-Original-Length: <bytes>``
    - ``X-Context-Compressed-Length: <bytes>``
    """

    def __init__(self, app, compressor: ContextCompressor):
        super().__init__(app)
        self._compressor = compressor

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method != "POST" or request.url.path not in _COMPRESSIBLE_PATHS:
            return await call_next(request)

        try:
            body = await request.body()
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return await call_next(request)

        compressed = False
        original_len = 0
        compressed_len = 0

        if request.url.path == "/v1/chat/completions":
            messages = payload.get("messages")
            if messages and isinstance(messages, list):
                new_messages, original_len, compressed_len = (
                    self._compressor.compress_messages(messages)
                )
                if compressed_len < original_len:
                    payload["messages"] = new_messages
                    compressed = True

        elif request.url.path == "/v1/completions":
            prompt = payload.get("prompt")
            if prompt and isinstance(prompt, str):
                new_prompt, original_len, compressed_len = (
                    self._compressor.compress_prompt(prompt)
                )
                if compressed_len < original_len:
                    payload["prompt"] = new_prompt
                    compressed = True

        # Replace the request body with the compressed payload
        if compressed:
            new_body = json.dumps(payload).encode()
            # Override the receive so downstream sees the new body
            request._body = new_body

        response = await call_next(request)

        # Add compression headers
        response.headers["X-Context-Compressed"] = str(compressed).lower()
        if compressed:
            response.headers["X-Context-Original-Length"] = str(original_len)
            response.headers["X-Context-Compressed-Length"] = str(compressed_len)

        return response
