"""Documented Sign in with ChatGPT OAuth for a single-user, self-hosted app.

The callback listener is loopback-only. On a headless host, forward its port over
SSH and open the authorization URL on the desktop. No browser token storage or
other application's auth cache is involved.
"""

import asyncio
import base64
import hashlib
import math
import secrets
import shlex
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

from .common import (
    ProviderError,
    prepare_private_dir,
    provider_failure,
    read_private_json,
    write_private_json,
)
from .transport import request_json

AUTH_ORIGIN = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_ORIGIN}/api/accounts/authorize"
TOKEN_URL = f"{AUTH_ORIGIN}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
TERMINAL_REFRESH_ERRORS = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}
TERMINAL_IDENTITY_ERRORS = {
    "invalid_identity",
    "account_mismatch",
    "plan_permission_missing",
    "invalid_token_response",
}


@dataclass
class PendingAuth:
    state: str
    nonce: str
    verifier: str
    client_id: str
    callback_uri: str
    authorization_url: str
    expires_at: float


class ChatGPTAuth:
    def __init__(
        self,
        private_dir: Path,
        http: httpx.AsyncClient,
        callback_port: int = 18455,
        callback_bind: str = "127.0.0.1",
        ssh_host: str = "your-server",
    ) -> None:
        prepare_private_dir(private_dir)
        self.path = private_dir / "chatgpt-auth.json"
        self.host_path = private_dir / "chatgpt-host.json"
        self.http = http
        self.callback_port = callback_port
        self.callback_bind = callback_bind
        self.ssh_host = ssh_host
        self.lock = asyncio.Lock()
        self.pending: PendingAuth | None = None
        self.server: asyncio.Server | None = None
        self.expiry_task: asyncio.Task[None] | None = None
        self.last_message = "Connect your ChatGPT account to enable AI."
        self._metadata: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0

    def status(self) -> tuple[bool, str]:
        record = read_private_json(self.path)
        connected = bool(record.get("credentials")) and PLAN_SCOPE in record.get("scopes", [])
        if connected:
            return True, "Connected. Eligible requests use your ChatGPT plan."
        return False, self.last_message

    def _host_id(self) -> str:
        host = read_private_json(self.host_path)
        if not host:
            host = {"host_id": f"urn:uuid:{uuid.uuid4()}"}
            write_private_json(self.host_path, host)
        value = host.get("host_id")
        if not isinstance(value, str) or not value.startswith("urn:uuid:"):
            raise ProviderError("invalid_host_id", "The saved app host identity is invalid.", 503)
        return value

    async def connect(self) -> dict[str, str]:
        async with self.lock:
            if self.pending is not None and self.pending.expires_at > time.time():
                return self._connect_response(self.pending)
            await self._stop_listener()
            record = read_private_json(self.path)
            client_id = str(record.get("client_id") or "dynamic_agent_client")
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            try:
                self.server = await asyncio.start_server(
                    self._serve_callback, self.callback_bind, self.callback_port, limit=8192
                )
            except OSError as exc:
                raise ProviderError(
                    "callback_port_unavailable",
                    "The OAuth callback port is busy. Change KCALLM_AUTH_PORT or close its owner.",
                    503,
                ) from exc
            port = self.server.sockets[0].getsockname()[1]
            callback_uri = f"http://127.0.0.1:{port}/auth/callback"
            parameters = {
                "client_id": client_id,
                "ext_agent_host_id": self._host_id(),
                "response_type": "code",
                "redirect_uri": callback_uri,
                "scope": SCOPES,
                "resource": RESOURCE,
                "state": state,
                "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
            if client_id == "dynamic_agent_client":
                parameters["agent_name_hint"] = "KcaLLM"
            # No id_token_hint: it would put a retained identity token in a URL.
            pending = PendingAuth(
                state,
                nonce,
                verifier,
                client_id,
                callback_uri,
                f"{AUTHORIZE_URL}?{urlencode(parameters)}",
                time.time() + 600,
            )
            self.pending = pending
            self.expiry_task = asyncio.create_task(self._expire(pending))
            self.last_message = "Complete sign-in in your desktop browser."
            return self._connect_response(pending)

    def _connect_response(self, pending: PendingAuth) -> dict[str, str]:
        port = urlsplit(pending.callback_uri).port
        return {
            "authorization_url": pending.authorization_url,
            "callback_uri": pending.callback_uri,
            "instructions": (
                "Open this link on the desktop. If KcaLLM runs on a remote server, first run "
                f"ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:{port}:127.0.0.1:{port} "
                f"{shlex.quote(self.ssh_host)} "
                "on this computer. Keep that terminal open until sign-in finishes. "
                "Use your configured SSH alias if it differs."
            ),
        }

    async def _expire(self, pending: PendingAuth) -> None:
        await asyncio.sleep(600)
        async with self.lock:
            if self.pending is pending:
                self.last_message = "Sign-in expired. Start a new connection attempt."
                await self._stop_listener()

    async def _stop_listener(self) -> None:
        self.pending = None
        if self.server is not None:
            self.server.close()
            # The callback connection is still writing its response. Waiting for all
            # clients here would deadlock that same callback on Python 3.13+.
            self.server = None
        if self.expiry_task is not None and self.expiry_task is not asyncio.current_task():
            self.expiry_task.cancel()
        self.expiry_task = None

    async def close(self) -> None:
        async with self.lock:
            await self._stop_listener()

    async def _serve_callback(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status, message = 400, "Invalid sign-in callback. Return to KcaLLM."
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            request_line = headers.split(b"\r\n", 1)[0].decode("ascii")
            method, target, _ = request_line.split(" ", 2)
            parsed = urlsplit(target)
            if method != "GET" or parsed.path != "/auth/callback":
                status, message = 404, "Not found."
            else:
                parameters = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=12)
                if any(len(values) != 1 for values in parameters.values()):
                    raise ProviderError("invalid_callback", "Invalid sign-in callback.", 400)
                await self.complete_callback({key: values[0] for key, values in parameters.items()})
                status = 200
                message = "ChatGPT connected. You can close this tab and return to KcaLLM."
        except ProviderError as exc:
            status, message = exc.status, exc.message
        except (
            ValueError,
            UnicodeError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        except Exception:
            # Never send provider responses or exception details into the callback page.
            status, message = 500, "Sign-in could not finish. Return to KcaLLM and try again."
        finally:
            body = message.encode()
            writer.write(
                f"HTTP/1.1 {status} Result\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                "Referrer-Policy: no-referrer\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def complete_callback(self, parameters: dict[str, str]) -> None:
        async with self.lock:
            pending = self.pending
            if pending is None or pending.expires_at < time.time():
                raise ProviderError("oauth_expired", "Sign-in expired. Start again in KcaLLM.", 400)
            if not secrets.compare_digest(parameters.get("state", ""), pending.state):
                # An unrelated callback must not consume the legitimate pending attempt.
                raise ProviderError("oauth_state_mismatch", "Sign-in state did not match.", 400)
            try:
                if parameters.get("error"):
                    raise ProviderError("oauth_declined", "ChatGPT connection was declined.", 400)
                code = parameters.get("code", "")
                if not code or len(code) > 4096:
                    raise ProviderError("invalid_callback", "No valid authorization code.", 400)
                issued = parameters.get("client_id", "")
                if pending.client_id == "dynamic_agent_client":
                    if not issued or issued == "dynamic_agent_client" or len(issued) > 200:
                        raise ProviderError(
                            "registration_incomplete", "Client registration failed."
                        )
                    client_id = issued
                else:
                    if issued and issued != pending.client_id:
                        raise ProviderError(
                            "client_mismatch", "The callback changed client ID.", 400
                        )
                    client_id = pending.client_id
                record = read_private_json(self.path)
                # Keep registration even if a one-time code expires before exchange succeeds.
                record["client_id"] = client_id
                write_private_json(self.path, record)
                token = await self._token_request(
                    {
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "code": code,
                        "code_verifier": pending.verifier,
                        "redirect_uri": pending.callback_uri,
                        "resource": RESOURCE,
                    }
                )
                identity = await self._verify_identity(
                    str(token.get("id_token", "")), client_id, pending.nonce
                )
                if record.get("subject") and record["subject"] != identity["sub"]:
                    raise ProviderError(
                        "account_mismatch",
                        "Reconnect using the originally registered account.",
                        400,
                    )
                credentials = self._credentials_from(token)
                scopes = self._scopes(token)
                record.update(
                    {
                        "subject": identity["sub"],
                        "email": identity.get("email", ""),
                        "id_token": token["id_token"],
                        "scopes": scopes,
                    }
                )
                record.pop("pending_refresh", None)
                if PLAN_SCOPE in scopes:
                    record["credentials"] = credentials
                    self.last_message = "ChatGPT connected."
                else:
                    record.pop("credentials", None)
                    self.last_message = "Signed in, but ChatGPT plan usage was not granted."
                write_private_json(self.path, record)
                if PLAN_SCOPE not in scopes:
                    raise ProviderError("plan_permission_missing", self.last_message, 403)
            except ProviderError as exc:
                self.last_message = exc.message
                raise
            finally:
                await self._stop_listener()

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            status, body = await request_json(self.http, "POST", TOKEN_URL, data=data, timeout=30)
        except httpx.HTTPError as exc:
            raise ProviderError(
                "auth_unavailable", "ChatGPT sign-in is temporarily unavailable."
            ) from exc
        if not 200 <= status < 300:
            error = body.get("error", "") if isinstance(body, dict) else ""
            code = error.get("code", "") if isinstance(error, dict) else error
            if not isinstance(code, str):
                code = ""
            if code in TERMINAL_REFRESH_ERRORS:
                raise ProviderError(
                    str(code), "The saved ChatGPT session requires sign-in again.", 401
                )
            raise provider_failure(status, body)
        if not isinstance(body, dict):
            raise ProviderError(
                "invalid_token_response", "ChatGPT returned an invalid token reply."
            )
        body["_received_at"] = time.time()
        return body

    @staticmethod
    def _scopes(token: dict[str, Any]) -> list[str]:
        scope = token.get("scope", "")
        if not isinstance(scope, str):
            raise ProviderError("invalid_token_response", "ChatGPT returned invalid permissions.")
        return scope.split()

    @staticmethod
    def _credentials_from(token: dict[str, Any]) -> dict[str, Any]:
        access, refresh = token.get("access_token"), token.get("refresh_token")
        expires = token.get("expires_in")
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
            or expires <= 0
            or str(token.get("token_type", "Bearer")).lower() != "bearer"
        ):
            raise ProviderError(
                "invalid_token_response", "ChatGPT returned incomplete credentials."
            )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": float(token.get("_received_at", time.time())) + expires,
            "earliest_refresh_at": token.get("earliest_refresh_at"),
        }

    async def _discovery(self) -> dict[str, Any]:
        if self._metadata is None:
            self._metadata = await self._get_json(f"{AUTH_ORIGIN}/.well-known/openid-configuration")
        return self._metadata

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            status, body = await request_json(self.http, "GET", url, timeout=10)
            if not 200 <= status < 300 or not isinstance(body, dict):
                raise ValueError("Expected object")
            return body
        except (httpx.HTTPError, ValueError, ProviderError) as exc:
            raise ProviderError(
                "identity_verification_unavailable", "ChatGPT identity verification is unavailable."
            ) from exc

    @staticmethod
    def _trusted_auth_url(value: Any) -> str:
        if not isinstance(value, str):
            raise ProviderError("invalid_auth_metadata", "Invalid ChatGPT identity configuration.")
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.netloc != "auth.openai.com":
            raise ProviderError("invalid_auth_metadata", "Invalid ChatGPT identity configuration.")
        return value

    async def _verify_identity(
        self, encoded: str, client_id: str, nonce: str | None = None
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(encoded)
            if header.get("alg") != "RS256":
                raise jwt.InvalidTokenError("Unexpected signature algorithm")
            metadata = await self._discovery()
            jwks_url = self._trusted_auth_url(metadata.get("jwks_uri"))
            key_data: dict[str, Any] | None = None
            for refresh in (False, True):
                if refresh or self._jwks is None or time.time() - self._jwks_at > 3600:
                    self._jwks = await self._get_json(jwks_url)
                    self._jwks_at = time.time()
                keys = self._jwks.get("keys", [])
                key_data = next(
                    (
                        key
                        for key in keys
                        if isinstance(key, dict) and key.get("kid") == header.get("kid")
                    ),
                    None,
                )
                if key_data is not None:
                    break
            if key_data is None:
                raise jwt.InvalidTokenError("Unknown signing key")
            key = jwt.PyJWK.from_dict(key_data, algorithm="RS256").key
            identity: dict[str, Any] = jwt.decode(
                encoded,
                key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=AUTH_ORIGIN,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
            if nonce is not None and not secrets.compare_digest(
                str(identity.get("nonce", "")), nonce
            ):
                raise jwt.InvalidTokenError("Nonce mismatch")
            return identity
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise ProviderError(
                "invalid_identity", "ChatGPT identity could not be verified.", 401
            ) from exc

    async def access_token(self) -> str:
        async with self.lock:
            record = read_private_json(self.path)
            credentials = record.get("credentials")
            if not isinstance(credentials, dict) or PLAN_SCOPE not in record.get("scopes", []):
                raise ProviderError("sign_in_required", "Connect ChatGPT in settings first.", 401)
            pending = record.get("pending_refresh")
            if isinstance(pending, dict):
                try:
                    await self._accept_refresh(record, pending)
                except ProviderError as exc:
                    if exc.code in TERMINAL_IDENTITY_ERRORS:
                        self._require_reconnect(record)
                        raise ProviderError(
                            "sign_in_required",
                            "ChatGPT renewal could not be verified. Reconnect in settings.",
                            401,
                        ) from exc
                    raise
                credentials = record["credentials"]
            if float(credentials.get("expires_at", 0)) <= time.time() + 60:
                earliest = self._earliest_refresh(credentials.get("earliest_refresh_at"))
                if earliest > time.time():
                    if float(credentials.get("expires_at", 0)) <= time.time():
                        raise ProviderError(
                            "refresh_not_ready", "ChatGPT renewal is not ready.", 503
                        )
                else:
                    try:
                        token = await self._token_request(
                            {
                                "grant_type": "refresh_token",
                                "client_id": str(record["client_id"]),
                                "refresh_token": str(credentials["refresh_token"]),
                                "resource": RESOURCE,
                            }
                        )
                        # Persist the replacement before JWKS validation, so a temporary network
                        # failure cannot lose the only usable rotating refresh token.
                        self._credentials_from(token)
                        record["pending_refresh"] = token
                        write_private_json(self.path, record)
                        await self._accept_refresh(record, token)
                        credentials = record["credentials"]
                    except ProviderError as exc:
                        if exc.code in TERMINAL_REFRESH_ERRORS | TERMINAL_IDENTITY_ERRORS:
                            self._require_reconnect(record)
                        raise
            token_value = credentials.get("access_token")
            if not isinstance(token_value, str) or not token_value:
                raise ProviderError("sign_in_required", "Reconnect ChatGPT in settings.", 401)
            return token_value

    def _require_reconnect(self, record: dict[str, Any]) -> None:
        for key in ("credentials", "pending_refresh", "id_token", "scopes"):
            record.pop(key, None)
        write_private_json(self.path, record)
        self.last_message = "ChatGPT session needs verification. Reconnect in settings."

    @staticmethod
    def _earliest_refresh(value: Any) -> float:
        if value is None:
            return 0
        try:
            if isinstance(value, str):
                try:
                    result = float(value)
                except ValueError:
                    result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            else:
                result = float(value)
            if not math.isfinite(result):
                raise ValueError("Invalid refresh time")
            return result
        except (ValueError, TypeError, OverflowError) as exc:
            raise ProviderError(
                "invalid_token_response", "ChatGPT returned an invalid renewal time."
            ) from exc

    async def _accept_refresh(self, record: dict[str, Any], token: dict[str, Any]) -> None:
        identity = await self._verify_identity(
            str(token.get("id_token", "")), str(record["client_id"])
        )
        if identity["sub"] != record.get("subject"):
            raise ProviderError(
                "account_mismatch", "ChatGPT renewal changed account identity.", 401
            )
        scopes = self._scopes(token)
        if PLAN_SCOPE not in scopes:
            raise ProviderError(
                "plan_permission_missing", "ChatGPT plan usage is no longer granted.", 403
            )
        record.update(
            {
                "credentials": self._credentials_from(token),
                "scopes": scopes,
                "id_token": token["id_token"],
            }
        )
        record.pop("pending_refresh", None)
        write_private_json(self.path, record)

    async def disconnect(self) -> None:
        async with self.lock:
            await self._stop_listener()
            record = read_private_json(self.path)
            credentials = record.get("credentials", {})
            pending = record.get("pending_refresh", {})
            refresh_token = pending.get("refresh_token") or credentials.get("refresh_token")
            revoked = not refresh_token
            if refresh_token:
                try:
                    metadata = await self._discovery()
                    endpoint = self._trusted_auth_url(metadata.get("revocation_endpoint"))
                    for attempt in range(2):
                        async with asyncio.timeout(10):
                            async with self.http.stream(
                                "POST",
                                endpoint,
                                timeout=10,
                                data={
                                    "token": refresh_token,
                                    "token_type_hint": "refresh_token",
                                    "client_id": str(record.get("client_id", "")),
                                },
                            ) as response:
                                status = response.status_code
                        if status == 200:
                            revoked = True
                            break
                        if status < 500:
                            break
                        if attempt == 0:
                            await asyncio.sleep(0.2)
                except (ProviderError, httpx.HTTPError, TimeoutError):
                    pass
            for key in ("credentials", "pending_refresh", "id_token", "scopes"):
                record.pop(key, None)
            write_private_json(self.path, record)
            self.last_message = (
                "ChatGPT disconnected."
                if revoked
                else "Disconnected locally. Remote revocation was not confirmed; "
                "disconnect KcaLLM in ChatGPT Settings."
            )
