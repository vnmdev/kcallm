"""Small shared pieces for private configuration and safe provider failures."""

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any


class ProviderError(Exception):
    def __init__(self, code: str, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def prepare_private_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        raise ProviderError("unsafe_storage", "Private storage must be a real directory.", 503)
    directory.chmod(0o700)


def read_private_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ProviderError("unsafe_storage", "Private files must not be symbolic links.", 503)
    if not path.exists():
        return {}
    try:
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Invalid private file")
        path.chmod(0o600)
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("Invalid private document")
        return value
    except (OSError, ValueError) as exc:
        raise ProviderError(
            "private_storage_unreadable",
            "Private configuration could not be read; it has not been replaced.",
            503,
        ) from exc


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    """Replace a complete record atomically, never exposing a partial token rotation."""
    prepare_private_dir(path.parent)
    if path.is_symlink():
        raise ProviderError("unsafe_storage", "Private files must not be symbolic links.", 503)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ProviderError(
            "private_storage_unwritable", "Private configuration could not be saved.", 503
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def provider_failure(response_status: int, body: Any) -> ProviderError:
    """Only known error codes are exposed; provider response bodies can contain secrets."""
    code = ""
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            code = str(error.get("code", ""))
        elif isinstance(error, str):
            code = error
    known = {
        "subscription_sharing_user_not_eligible": (
            "This account or workspace is not eligible for ChatGPT plan usage.",
            403,
        ),
        "subscription_sharing_usage_limit_exceeded": (
            "ChatGPT plan usage has reached a limit. Review ChatGPT Settings → Usage.",
            429,
        ),
        "subscription_sharing_usage_unavailable": (
            "ChatGPT usage availability could not be checked. Try again later.",
            503,
        ),
        "subscription_sharing_unsupported_capability": (
            "The selected model does not support this request capability.",
            422,
        ),
        "subscription_sharing_route_not_supported": (
            "ChatGPT plan access does not support this request route.",
            403,
        ),
        "subscription_sharing_invalid_user": (
            "ChatGPT could not validate this account connection.",
            401,
        ),
        "chatpass_v2_scope_not_authorized": (
            "The ChatGPT connection does not authorize plan usage.",
            403,
        ),
        "chatpass_v2_invalid_authorization_context": (
            "The ChatGPT connection does not authorize this request.",
            403,
        ),
    }
    if code in known:
        message, status = known[code]
        return ProviderError(code, message, status)
    if response_status == 401:
        return ProviderError("provider_unauthorized", "The provider rejected its credentials.", 401)
    if response_status == 403:
        return ProviderError(
            "provider_forbidden",
            "Provider policy or account eligibility blocked this request.",
            403,
        )
    if response_status == 429:
        return ProviderError("provider_rate_limited", "Provider usage is currently limited.", 429)
    return ProviderError("provider_error", "The provider could not complete the request.")
