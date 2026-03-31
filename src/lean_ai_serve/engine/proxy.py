"""Async reverse proxy — forwards OpenAI-compatible requests to vLLM."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

# Reusable client for proxying — long timeout for generation
_proxy_client: httpx.AsyncClient | None = None


def get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
    return _proxy_client


async def close_proxy_client() -> None:
    global _proxy_client
    if _proxy_client is not None:
        await _proxy_client.aclose()
        _proxy_client = None


async def proxy_request(
    request: Request,
    target_port: int,
    path: str,
    *,
    on_usage: Callable[[dict], None] | None = None,
) -> StreamingResponse | JSONResponse:
    """Forward a request to a vLLM backend.

    Handles both streaming (SSE) and non-streaming responses.

    Args:
        on_usage: Optional callback invoked with the ``usage`` dict
            extracted from the vLLM response (``{prompt_tokens, completion_tokens,
            total_tokens}``).  For non-streaming responses it is called before
            the ``JSONResponse`` is returned.  For streaming responses it is
            called when the final SSE chunk containing usage data is yielded.
    """
    client = get_proxy_client()
    target_url = f"http://127.0.0.1:{target_port}{path}"

    body = await request.body()

    # Build upstream request
    headers = {
        "content-type": request.headers.get("content-type", "application/json"),
        "accept": request.headers.get("accept", "application/json"),
    }

    try:
        # Check if the request wants streaming
        is_streaming = False
        if body:
            try:
                payload = json.loads(body)
                is_streaming = payload.get("stream", False)
            except (json.JSONDecodeError, AttributeError):
                pass

        if is_streaming:
            return await _proxy_streaming(
                client, target_url, headers, body, on_usage=on_usage
            )
        else:
            return await _proxy_json(
                client, target_url, headers, body, on_usage=on_usage
            )

    except httpx.ConnectError:
        logger.error("Cannot connect to vLLM at port %d", target_port)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Model backend unavailable", "type": "server_error"}},
        )
    except httpx.ReadTimeout:
        logger.error("Read timeout from vLLM at port %d", target_port)
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "Model backend timeout", "type": "server_error"}},
        )


async def _proxy_json(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    body: bytes,
    *,
    on_usage: Callable[[dict], None] | None = None,
) -> JSONResponse:
    """Forward a non-streaming request."""
    resp = await client.post(url, content=body, headers=headers)
    content = resp.json()

    # Extract usage data if available
    if on_usage and isinstance(content, dict) and "usage" in content:
        try:
            on_usage(content["usage"])
        except Exception:
            logger.debug("Failed to process usage callback", exc_info=True)

    return JSONResponse(status_code=resp.status_code, content=content)


async def _proxy_streaming(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    body: bytes,
    *,
    on_usage: Callable[[dict], None] | None = None,
) -> StreamingResponse:
    """Forward a streaming (SSE) request."""
    req = client.build_request("POST", url, content=body, headers=headers)
    resp = await client.send(req, stream=True)

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
                # Try to extract usage from SSE data lines
                if on_usage and b'"usage"' in chunk:
                    _extract_sse_usage(chunk, on_usage)
        finally:
            await resp.aclose()

    return StreamingResponse(
        event_stream(),
        status_code=resp.status_code,
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
            "x-accel-buffering": "no",
        },
    )


def _extract_sse_usage(chunk: bytes, on_usage: Callable[[dict], None]) -> None:
    """Parse SSE chunk bytes looking for a usage field in data lines."""
    try:
        for line in chunk.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data: "):
                continue
            data_str = line[6:].decode(errors="replace")
            if data_str == "[DONE]":
                continue
            data = json.loads(data_str)
            if "usage" in data and data["usage"]:
                on_usage(data["usage"])
                return
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
        pass
    except Exception:
        logger.debug("SSE usage extraction error", exc_info=True)
