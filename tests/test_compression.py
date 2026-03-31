"""Tests for context compression middleware."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from lean_ai_serve.config import ContextCompressionConfig  # noqa: I001
from lean_ai_serve.middleware.compression import (
    CompressionMiddleware,
    ContextCompressor,
)

# ---------------------------------------------------------------------------
# ContextCompressor unit tests
# ---------------------------------------------------------------------------


class TestContextCompressor:
    def _make_compressor(self, min_length: int = 100, target_ratio: float = 0.5):
        config = ContextCompressionConfig(
            enabled=True, method="llmlingua2", target_ratio=target_ratio, min_length=min_length
        )
        return ContextCompressor(config)

    def test_short_text_not_compressed(self):
        """Text below min_length passes through unchanged."""
        c = self._make_compressor(min_length=1000)
        result = c.compress("Short text")
        assert result == "Short text"

    def test_compress_calls_llmlingua(self):
        """Long text triggers the LLMlingua2 compressor."""
        c = self._make_compressor(min_length=10)
        mock_compressor = MagicMock()
        mock_compressor.compress_prompt.return_value = {"compressed_prompt": "compressed"}
        c._compressor = mock_compressor
        c._initialized = True

        result = c.compress("A" * 100)
        assert result == "compressed"
        mock_compressor.compress_prompt.assert_called_once()

    def test_compress_fallback_on_error(self):
        """If compression fails, original text is returned."""
        c = self._make_compressor(min_length=10)
        mock_compressor = MagicMock()
        mock_compressor.compress_prompt.side_effect = RuntimeError("boom")
        c._compressor = mock_compressor
        c._initialized = True

        result = c.compress("A" * 100)
        assert result == "A" * 100

    def test_compress_fallback_when_unavailable(self):
        """If LLMlingua not installed, original text is returned."""
        c = self._make_compressor(min_length=10)
        c._initialized = True  # Mark as loaded but compressor is None
        c._compressor = None

        result = c.compress("A" * 100)
        assert result == "A" * 100

    def test_compress_messages_preserves_recent(self):
        """Last N messages are preserved uncompressed."""
        c = self._make_compressor(min_length=10)
        mock_compressor = MagicMock()
        mock_compressor.compress_prompt.return_value = {"compressed_prompt": "short"}
        c._compressor = mock_compressor
        c._initialized = True

        messages = [
            {"role": "system", "content": "A" * 200},
            {"role": "user", "content": "B" * 200},
            {"role": "assistant", "content": "C" * 200},
            {"role": "user", "content": "D" * 200},
        ]
        result, orig_len, comp_len = c.compress_messages(messages)

        # Last 2 should be preserved
        assert result[-1]["content"] == "D" * 200
        assert result[-2]["content"] == "C" * 200
        # Earlier messages should be compressed
        assert result[0]["content"] == "short"
        assert result[1]["content"] == "short"

    def test_compress_messages_empty_list(self):
        """Empty message list returns unchanged."""
        c = self._make_compressor()
        result, orig, comp = c.compress_messages([])
        assert result == []
        assert orig == 0
        assert comp == 0

    def test_compress_messages_all_short(self):
        """Messages below min_length are not compressed."""
        c = self._make_compressor(min_length=1000)
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result, orig, comp = c.compress_messages(messages)
        assert result == messages
        assert orig == comp

    def test_compress_prompt_long(self):
        """Plain text prompt compression returns correct lengths."""
        c = self._make_compressor(min_length=10)
        mock_compressor = MagicMock()
        mock_compressor.compress_prompt.return_value = {"compressed_prompt": "short"}
        c._compressor = mock_compressor
        c._initialized = True

        result, orig, comp = c.compress_prompt("A" * 100)
        assert result == "short"
        assert orig == 100
        assert comp == 5

    def test_compress_prompt_short(self):
        """Short prompt passes through unchanged."""
        c = self._make_compressor(min_length=1000)
        result, orig, comp = c.compress_prompt("Hello")
        assert result == "Hello"
        assert orig == comp == 5

    def test_lazy_loading_import_error(self):
        """Missing llmlingua triggers warning but doesn't crash."""
        c = self._make_compressor(min_length=10)
        with (
            patch.dict("sys.modules", {"llmlingua": None}),
            patch("lean_ai_serve.middleware.compression.logger"),
        ):
            c._ensure_loaded()
            assert c._compressor is None
            assert c._initialized is True


# ---------------------------------------------------------------------------
# CompressionMiddleware integration tests (via FastAPI TestClient)
# ---------------------------------------------------------------------------


@pytest.fixture
def compression_app():
    """Create a minimal Starlette app with compression middleware for testing."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    config = ContextCompressionConfig(
        enabled=True, method="llmlingua2", target_ratio=0.5, min_length=50
    )
    compressor = ContextCompressor(config)

    # Mock the LLMlingua compressor to return deterministic output
    mock = MagicMock()
    mock.compress_prompt.return_value = {"compressed_prompt": "compressed-text"}
    compressor._compressor = mock
    compressor._initialized = True

    async def chat(request):
        body = await request.body()
        return JSONResponse(json.loads(body))

    async def completions(request):
        body = await request.body()
        return JSONResponse(json.loads(body))

    async def models(request):
        return JSONResponse({"data": []})

    app = Starlette(
        routes=[
            Route("/v1/chat/completions", chat, methods=["POST"]),
            Route("/v1/completions", completions, methods=["POST"]),
            Route("/v1/models", models, methods=["GET"]),
        ],
    )
    app.add_middleware(CompressionMiddleware, compressor=compressor)

    return app, compressor


class TestCompressionMiddleware:
    def test_chat_completions_compressed(self, compression_app):
        """Chat completions with long messages triggers compression."""
        app, _ = compression_app
        client = TestClient(app)

        payload = {
            "model": "test",
            "messages": [
                {"role": "system", "content": "A" * 200},
                {"role": "user", "content": "B" * 200},
                {"role": "assistant", "content": "C" * 200},
                {"role": "user", "content": "Hello"},
            ],
        }
        resp = client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200
        assert resp.headers["x-context-compressed"] == "true"
        assert "x-context-original-length" in resp.headers

        # The first two messages should have been compressed
        body = resp.json()
        assert body["messages"][0]["content"] == "compressed-text"

    def test_completions_compressed(self, compression_app):
        """Plain completions with long prompt triggers compression."""
        app, _ = compression_app
        client = TestClient(app)

        payload = {"model": "test", "prompt": "X" * 200}
        resp = client.post("/v1/completions", json=payload)
        assert resp.status_code == 200
        assert resp.headers["x-context-compressed"] == "true"
        body = resp.json()
        assert body["prompt"] == "compressed-text"

    def test_short_payload_not_compressed(self, compression_app):
        """Short messages are not compressed."""
        app, _ = compression_app
        client = TestClient(app)

        payload = {
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        resp = client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200
        assert resp.headers["x-context-compressed"] == "false"

    def test_non_compressible_path_passthrough(self, compression_app):
        """Non-compressible paths are not intercepted."""
        app, _ = compression_app
        client = TestClient(app)

        resp = client.get("/v1/models")
        assert resp.status_code == 200
        assert "x-context-compressed" not in resp.headers

    def test_invalid_json_passthrough(self, compression_app):
        """Invalid JSON body is passed through without middleware crash."""
        app, _ = compression_app
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/chat/completions",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
        # Middleware passes through; endpoint may crash on json.loads (500)
        assert resp.status_code == 500

    def test_recent_messages_preserved(self, compression_app):
        """Last N messages are not compressed."""
        app, _ = compression_app
        client = TestClient(app)

        long = "Z" * 200
        payload = {
            "model": "test",
            "messages": [
                {"role": "system", "content": long},
                {"role": "user", "content": long},
                {"role": "assistant", "content": long},
                {"role": "user", "content": long},
            ],
        }
        resp = client.post("/v1/chat/completions", json=payload)
        body = resp.json()

        # Last 2 preserved
        assert body["messages"][-1]["content"] == long
        assert body["messages"][-2]["content"] == long
        # First 2 compressed
        assert body["messages"][0]["content"] == "compressed-text"
        assert body["messages"][1]["content"] == "compressed-text"
