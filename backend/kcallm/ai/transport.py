"""Bound provider reads before JSON or line parsing can buffer an entire response."""

import asyncio
import json
from typing import Any

import httpx

from .common import ProviderError

MAX_JSON_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 16 * 1024


def require_identity_encoding(response: httpx.Response) -> None:
    # Requests explicitly negotiate identity. Avoid allocating an arbitrarily
    # large decoded gzip chunk before the application can apply its byte cap.
    if response.headers.get("content-encoding", "identity").strip().lower() not in {
        "",
        "identity",
    }:
        raise ProviderError(
            "invalid_provider_response", "The provider ignored the requested transfer encoding."
        )


async def read_json(response: httpx.Response) -> Any:
    require_identity_encoding(response)
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAX_JSON_BYTES:
            raise ProviderError("provider_output_too_large", "The provider reply was too large.")
        body.extend(chunk)
    try:
        return json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise ProviderError(
            "invalid_provider_response", "The provider returned an invalid reply."
        ) from exc


async def request_json(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    timeout: float = 30,
    **kwargs: Any,
) -> tuple[int, Any]:
    """Both the total duration and decoded body size are bounded, including errors."""
    headers = httpx.Headers(kwargs.pop("headers", {}))
    headers["Accept-Encoding"] = "identity"
    try:
        async with asyncio.timeout(timeout):
            async with http.stream(
                method, url, timeout=timeout, headers=headers, **kwargs
            ) as response:
                return response.status_code, await read_json(response)
    except TimeoutError as exc:
        raise ProviderError(
            "provider_timeout", "The provider took too long. Your draft has not been saved.", 504
        ) from exc
