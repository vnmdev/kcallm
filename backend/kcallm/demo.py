"""Public demo mode: periodic reset to a synthetic catalogue and AI request limits.

Enabled with KCALLM_DEMO=1. The demo never holds personal data or provider sign-ins;
AI requests go to one hosted model whose key and spend cap are configured server-side.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request

from .models import FoodInput, Nutrients
from .storage import Store

DAY = 86_400.0
HOUR = 3_600.0


def demo_enabled() -> bool:
    return os.getenv("KCALLM_DEMO") == "1"


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number.") from exc
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}.")
    return value


@dataclass(frozen=True)
class DemoConfig:
    seed_path: Path
    reset_minutes: int
    ai_per_ip_per_hour: int
    ai_global_per_day: int
    model: str

    @classmethod
    def from_env(cls, project_root: Path) -> DemoConfig:
        return cls(
            seed_path=Path(
                os.getenv("KCALLM_DEMO_SEED", str(project_root / "examples" / "demo-seed.json"))
            ),
            reset_minutes=_int_env("KCALLM_DEMO_RESET_MINUTES", 60, 5, 1_440),
            ai_per_ip_per_hour=_int_env("KCALLM_DEMO_AI_PER_HOUR", 15, 1, 1_000),
            ai_global_per_day=_int_env("KCALLM_DEMO_AI_PER_DAY", 300, 1, 100_000),
            model=os.getenv("KCALLM_DEMO_MODEL", "").strip(),
        )


def load_seed(path: Path) -> tuple[list[FoodInput], Nutrients]:
    """Read a demo seed: either a list of foods, or {"goals": {...}, "foods": [...]}."""
    data = json.loads(path.read_text())
    if isinstance(data, list):
        data = {"foods": data}
    foods = [FoodInput.model_validate(item) for item in data.get("foods", [])]
    goals = Nutrients.model_validate(data.get("goals", {}))
    return foods, goals


class DemoResetter:
    """Resets the store to the seed when the interval has elapsed (checked per API request)."""

    def __init__(
        self, store: Store, config: DemoConfig, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.store = store
        self.interval = config.reset_minutes * 60
        self.foods, self.goals = load_seed(config.seed_path)
        self.clock = clock
        self.last_reset: float | None = None
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.store.reset_to_seed(self.foods, self.goals)
            self.last_reset = self.clock()

    def maybe_reset(self) -> None:
        if self.last_reset is not None and self.clock() - self.last_reset < self.interval:
            return
        with self.lock:
            # Another request may have reset while this one waited for the lock.
            if self.last_reset is not None and self.clock() - self.last_reset < self.interval:
                return
            self.store.reset_to_seed(self.foods, self.goals)
            self.last_reset = self.clock()


class RateLimiter:
    """Sliding-window limits: per client IP per hour, and across all clients per day."""

    def __init__(
        self,
        per_ip_per_hour: int,
        global_per_day: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_ip_per_hour = per_ip_per_hour
        self.global_per_day = global_per_day
        self.clock = clock
        self.by_ip: dict[str, deque[float]] = {}
        self.all: deque[float] = deque()
        self.lock = threading.Lock()

    def check(self, ip: str) -> str | None:
        """Record the request and return None, or return why it is refused."""
        now = self.clock()
        with self.lock:
            while self.all and now - self.all[0] >= DAY:
                self.all.popleft()
            hits = self.by_ip.setdefault(ip, deque())
            while hits and now - hits[0] >= HOUR:
                hits.popleft()
            if len(self.all) >= self.global_per_day:
                return (
                    "The demo's AI allowance for today is used up. "
                    "Manual entry still works; please try again tomorrow."
                )
            if len(hits) >= self.per_ip_per_hour:
                return (
                    f"Demo limit reached: {self.per_ip_per_hour} AI requests per hour. "
                    "Manual entry still works."
                )
            hits.append(now)
            self.all.append(now)
            if len(self.by_ip) > 10_000:
                for key in [key for key, value in self.by_ip.items() if not value]:
                    del self.by_ip[key]
            return None


def client_ip(request: Request) -> str:
    """The last X-Forwarded-For hop is the one added by our own reverse proxy (Caddy)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip() or "unknown"
    return request.client.host if request.client else "unknown"
