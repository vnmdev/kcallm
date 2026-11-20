"""Two bounded inference transports, producing proposals rather than actions."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .auth import RESOURCE, ChatGPTAuth
from .common import ProviderError, provider_failure
from .schemas import AIInput
from .transport import read_json, request_json, require_identity_encoding

MAX_PROVIDER_OUTPUT = 256 * 1024
MAX_STREAM_BYTES = 4 * 1024 * 1024


def normalize_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid endpoint")
        _ = parsed.port
    except ValueError as exc:
        raise ProviderError(
            "invalid_endpoint",
            "Use an HTTP(S) base URL without credentials, query or fragment.",
            422,
        ) from exc
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Responses strict schemas require all properties, including nullable ones."""
    result: dict[str, Any] = json.loads(json.dumps(schema))

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(result)
    return result


def llama_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep llama.cpp's grammar small; application validation remains authoritative.

    Large string repetition bounds fail grammar initialization on the deployed
    server. Nested references also have documented converter limitations, so
    expand our finite local definitions before sending the grammar.
    """
    strict = strict_json_schema(schema)
    definitions = strict.get("$defs", {})

    def expand(value: Any) -> Any:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                return expand(definitions[reference.removeprefix("#/$defs/")])
            return {
                key: expand(item)
                for key, item in value.items()
                if key not in {"$defs", "title", "minLength", "maxLength"}
            }
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    result: dict[str, Any] = expand(strict)
    return result


def parse_event(data: list[bytes]) -> dict[str, Any] | None:
    raw = b"\n".join(data)
    if not raw or raw == b"[DONE]":
        return None
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ProviderError("invalid_stream", "The model stream was malformed.") from exc
    if not isinstance(event, dict):
        raise ProviderError("invalid_stream", "The model stream was malformed.")
    return event


async def sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    require_identity_encoding(response)
    data: list[bytes] = []
    buffer = bytearray()
    event_bytes = total_bytes = 0
    async for chunk in response.aiter_bytes():
        total_bytes += len(chunk)
        if total_bytes > MAX_STREAM_BYTES:
            raise ProviderError("provider_output_too_large", "The model stream was too large.")
        buffer.extend(chunk)
        while (newline := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:newline]).removesuffix(b"\r")
            del buffer[: newline + 1]
            event_bytes += len(line)
            if event_bytes > MAX_PROVIDER_OUTPUT:
                raise ProviderError("provider_output_too_large", "The model event was too large.")
            if not line:
                event = parse_event(data)
                if event is not None:
                    yield event
                data, event_bytes = [], 0
            elif line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" "))
        if event_bytes + len(buffer) > MAX_PROVIDER_OUTPUT:
            raise ProviderError("provider_output_too_large", "The model line was too large.")
    if buffer.startswith(b"data:"):
        data.append(bytes(buffer[5:]).lstrip(b" "))
    event = parse_event(data)
    if event is not None:
        yield event


class Providers:
    def __init__(
        self,
        http: httpx.AsyncClient,
        auth: ChatGPTAuth,
        llama_key: str = "",
        generation_timeout: float = 180,
    ) -> None:
        self.http = http
        self.auth = auth
        self.llama_headers = {"Authorization": f"Bearer {llama_key}"} if llama_key else {}
        self.generation_timeout = generation_timeout

    async def models(self, provider: str, llama_base_url: str) -> list[dict[str, Any]]:
        if provider == "chatgpt":
            token = await self.auth.access_token()
            status, body = await request_json(
                self.http,
                "GET",
                f"{RESOURCE}/models",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        else:
            status, body = await request_json(
                self.http, "GET", f"{llama_base_url}/models", headers=self.llama_headers, timeout=3
            )
        if not 200 <= status < 300:
            raise provider_failure(status, body)
        if not isinstance(body, dict):
            raise ProviderError("invalid_model_catalogue", "The model catalogue was invalid.")
        raw_models = body.get("models", body.get("data", []))
        if not isinstance(raw_models, list):
            raise ProviderError("invalid_model_catalogue", "The model catalogue was invalid.")
        models: list[dict[str, Any]] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            if provider == "chatgpt" and item.get("visibility", "list") != "list":
                continue
            identifier = item.get("slug", item.get("id"))
            if not isinstance(identifier, str) or not identifier:
                continue
            modalities = item.get("input_modalities", item.get("inputModalities", []))
            architecture = item.get("architecture", {})
            if not modalities and isinstance(architecture, dict):
                modalities = architecture.get("input_modalities", [])
            meta = item.get("meta", {})
            capabilities = item.get("capabilities", [])
            vision = isinstance(modalities, list) and "image" in modalities
            if isinstance(capabilities, dict):
                vision = vision or bool(capabilities.get("vision") or capabilities.get("image"))
            elif isinstance(capabilities, list):
                vision = vision or any(
                    value in {"vision", "image", "multimodal"}
                    for value in capabilities
                    if isinstance(value, str)
                )
            multimodal = item.get(
                "multimodal", meta.get("multimodal") if isinstance(meta, dict) else None
            )
            if isinstance(multimodal, dict):
                vision = vision or bool(multimodal.get("vision") or multimodal.get("image"))
            elif multimodal is True:
                vision = True
            models.append(
                {
                    "id": identifier,
                    "name": str(item.get("display_name", item.get("name", identifier))),
                    "input_modalities": ["text", "image"] if vision else ["text"],
                }
            )
        return models

    async def generate(
        self,
        provider: str,
        model: str,
        base_url: str,
        request: AIInput,
        instructions: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> str:
        try:
            async with asyncio.timeout(self.generation_timeout):
                if provider == "chatgpt":
                    return await self._chatgpt(model, request, instructions, schema, schema_name)
                return await self._llama(
                    base_url, model, request, instructions, schema, schema_name
                )
        except TimeoutError as exc:
            raise ProviderError(
                "provider_timeout",
                "The provider took too long. Your draft has not been saved.",
                504,
            ) from exc

    async def _chatgpt(
        self,
        model: str,
        request: AIInput,
        instructions: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> str:
        token = await self.auth.access_token()
        content: list[dict[str, str]] = []
        if request.text:
            content.append({"type": "input_text", "text": request.text})
        if request.image_data_url:
            content.append({"type": "input_image", "image_url": request.image_data_url})
        payload = {
            "model": model,
            "instructions": instructions,
            "input": [{"role": "user", "content": content}],
            "store": False,
            "stream": True,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": strict_json_schema(schema),
                    "strict": True,
                }
            },
        }
        chunks: list[str] = []
        output_size = 0
        completed = False
        async with self.http.stream(
            "POST",
            f"{RESOURCE}/responses",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
        ) as response:
            if not response.is_success:
                raise provider_failure(response.status_code, await read_json(response))
            async for event in sse_events(response):
                event_type = event.get("type")
                if event_type == "response.output_text.delta":
                    delta = event.get("delta", "")
                    if not isinstance(delta, str):
                        raise ProviderError("invalid_stream", "The model stream was malformed.")
                    output_size += len(delta.encode("utf-8"))
                    if output_size > MAX_PROVIDER_OUTPUT:
                        raise ProviderError(
                            "provider_output_too_large", "The model reply was too large."
                        )
                    chunks.append(delta)
                elif event_type == "response.completed":
                    result = event.get("response", {})
                    if (
                        isinstance(result, dict)
                        and result.get("status", "completed") != "completed"
                    ):
                        raise ProviderError(
                            "incomplete_response", "The model did not complete its reply."
                        )
                    completed = True
                    break
                elif event_type in {"response.failed", "error"}:
                    raise provider_failure(502, event.get("response", event))
                elif event_type in {"response.incomplete", "response.refusal.delta"}:
                    raise ProviderError(
                        "incomplete_response", "The model did not complete its reply."
                    )
        if not completed or not chunks:
            raise ProviderError(
                "incomplete_response", "The model connection ended before a complete reply arrived."
            )
        return "".join(chunks)

    async def _llama(
        self,
        base_url: str,
        model: str,
        request: AIInput,
        instructions: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> str:
        content: list[dict[str, Any]] = []
        if request.text:
            content.append({"type": "text", "text": request.text})
        if request.image_data_url:
            content.append({"type": "image_url", "image_url": {"url": request.image_data_url}})
        status, body = await request_json(
            self.http,
            "POST",
            f"{base_url}/chat/completions",
            timeout=self.generation_timeout,
            headers=self.llama_headers,
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": instructions
                        + "\nReturn JSON matching this schema:\n"
                        + json.dumps(llama_json_schema(schema), separators=(",", ":")),
                    },
                    {"role": "user", "content": content},
                ],
                "stream": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": llama_json_schema(schema),
                    },
                },
                "temperature": 0,
                "max_tokens": 4096,
            },
        )
        if not 200 <= status < 300:
            raise provider_failure(status, body)
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"]
            if choice.get("finish_reason") != "stop" or not isinstance(text, str) or not text:
                raise ValueError("Incomplete completion")
            if len(text) > MAX_PROVIDER_OUTPUT:
                raise ValueError("Oversized completion")
            return text
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(
                "incomplete_response", "The local model did not finish a valid reply."
            ) from exc
