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
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Request

from .models import ConfirmRequest, FoodInput, Nutrients, ReviewedLine
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


@dataclass(frozen=True)
class DiarySeed:
    food: str
    quantity: str
    unit: str


def load_seed(path: Path) -> tuple[list[FoodInput], Nutrients, list[DiarySeed]]:
    """Read a demo seed: a list of foods, or {"goals": {...}, "foods": [...], "diary": [...]}.

    Diary items name a seeded food and an amount; they are logged for "today" on each reset
    so the demo never opens on an empty day.
    """
    data = json.loads(path.read_text())
    if isinstance(data, list):
        data = {"foods": data}
    foods = [FoodInput.model_validate(item) for item in data.get("foods", [])]
    goals = Nutrients.model_validate(data.get("goals", {}))
    names = {food.name for food in foods}
    diary = [
        DiarySeed(str(item["food"]), str(item["quantity"]), str(item["unit"]))
        for item in data.get("diary", [])
    ]
    unknown = [item.food for item in diary if item.food not in names]
    if unknown:
        raise ValueError(f"Demo diary refers to foods not in the seed: {', '.join(unknown)}")
    return foods, goals, diary


class DemoResetter:
    """Resets the store to the seed when the interval has elapsed (checked per API request)."""

    def __init__(
        self, store: Store, config: DemoConfig, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.store = store
        self.interval = config.reset_minutes * 60
        self.foods, self.goals, self.diary = load_seed(config.seed_path)
        self.clock = clock
        self.last_reset: float | None = None
        self.lock = threading.Lock()

    def _reset_now(self) -> None:
        self.store.reset_to_seed(self.foods, self.goals)
        if self.diary:
            by_name = {food.name: food for food in self.store.foods()}
            today = datetime.now(ZoneInfo(self.store.settings().timezone)).date()
            self.store.confirm(
                ConfirmRequest(
                    request_id=uuid4(),
                    date=today,
                    items=[
                        ReviewedLine(
                            food_id=by_name[item.food].id,
                            food_revision=by_name[item.food].revision,
                            quantity=Decimal(item.quantity),
                            unit=item.unit,
                        )
                        for item in self.diary
                    ],
                )
            )
        self.last_reset = self.clock()

    def reset(self) -> None:
        with self.lock:
            self._reset_now()

    def maybe_reset(self) -> None:
        if self.last_reset is not None and self.clock() - self.last_reset < self.interval:
            return
        with self.lock:
            # Another request may have reset while this one waited for the lock.
            if self.last_reset is not None and self.clock() - self.last_reset < self.interval:
                return
            self._reset_now()


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
