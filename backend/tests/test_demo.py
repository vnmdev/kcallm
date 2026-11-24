"""Public demo mode: seeded resets, locked AI settings, rate limits and disabled restores."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from kcallm.app import create_app
from kcallm.demo import DemoConfig, DemoResetter, RateLimiter, load_seed
from kcallm.storage import Store

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "examples" / "demo-seed.json"


@pytest.fixture
def demo_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KCALLM_DEMO", "1")
    monkeypatch.setenv("KCALLM_DEMO_MODEL", "provider/demo-vision-model")
    monkeypatch.setenv("KCALLM_DEMO_SEED", str(SEED))
    monkeypatch.setenv("KCALLM_DEMO_AI_PER_HOUR", "2")
    monkeypatch.setenv("KCALLM_PRIVATE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("KCALLM_ALLOWED_ORIGINS", "https://kcallm.test")
    monkeypatch.setenv("KCALLM_ALLOWED_HOSTS", "testserver")
    return tmp_path


def test_seed_file_is_valid_and_realistic() -> None:
    foods, goals, diary = load_seed(SEED)
    assert len(foods) >= 10
    assert all(food.nutrients.kcal is not None for food in foods)
    assert goals.kcal is not None
    assert diary, "the demo should open on a day with entries"


def test_seed_rejects_diary_items_for_unknown_foods(tmp_path: Path) -> None:
    seed = json.loads(SEED.read_text())
    seed["diary"].append({"food": "Unicorn steak", "quantity": "1", "unit": "g"})
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed))
    with pytest.raises(ValueError, match="Unicorn steak"):
        load_seed(path)


def test_each_reset_starts_with_todays_diary_filled_in(demo_env: Path) -> None:
    client = TestClient(create_app(demo_env / "data", include_ai=False))
    today = datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    day = client.get("/api/diary", params={"date": today}).json()
    _, _, diary = load_seed(SEED)
    assert len(day["entries"]) == len(diary)
    assert int(float(day["totals"]["kcal"])) > 0


def test_demo_starts_with_the_seed_catalogue_and_goals(demo_env: Path) -> None:
    client = TestClient(create_app(demo_env / "data", include_ai=False))
    foods = client.get("/api/foods").json()
    assert {food["name"] for food in foods} >= {"Banana", "Chicken breast", "Rolled oats"}
    assert client.get("/api/settings").json()["goals"]["kcal"] == "2200"
    assert client.get("/api/demo").json() == {"demo": True, "reset_minutes": 60, "ai_per_hour": 2}


def test_restore_and_starter_drafts_are_disabled_in_demo(demo_env: Path) -> None:
    client = TestClient(create_app(demo_env / "data", include_ai=False))
    export = client.get("/api/export").json()
    headers = {"Origin": "https://kcallm.test"}
    response = client.post("/api/restore/preview", json={"data": export}, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "demo_locked"
    response = client.post("/api/restore", json={"data": export, "replace": True}, headers=headers)
    assert response.status_code == 403
    assert client.get("/api/starter-drafts").json() == []


def test_demo_info_is_false_outside_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KCALLM_DEMO", raising=False)
    monkeypatch.setenv("KCALLM_PRIVATE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("KCALLM_ALLOWED_HOSTS", "testserver")
    client = TestClient(create_app(tmp_path / "data", include_ai=False))
    assert client.get("/api/demo").json() == {"demo": False}
    assert client.get("/api/foods").json() == []


def test_resetter_restores_the_seed_after_the_interval(tmp_path: Path) -> None:
    now = [0.0]
    store = Store(tmp_path)
    config = DemoConfig(
        SEED, reset_minutes=60, ai_per_ip_per_hour=5, ai_global_per_day=50, model="m"
    )
    resetter = DemoResetter(store, config, clock=lambda: now[0])
    resetter.reset()
    seeded = len(store.foods())
    banana = next(food for food in store.foods() if food.name == "Banana")
    store.archive_food(banana.id, banana.revision)
    assert len(store.foods()) == seeded - 1

    now[0] = 59 * 60
    resetter.maybe_reset()
    assert len(store.foods()) == seeded - 1  # not yet

    now[0] = 60 * 60
    resetter.maybe_reset()
    assert len(store.foods()) == seeded
    assert len(store.foods(include_archived=True)) == seeded


def test_rate_limiter_per_ip_and_global() -> None:
    now = [0.0]
    limiter = RateLimiter(per_ip_per_hour=2, global_per_day=3, clock=lambda: now[0])
    assert limiter.check("1.1.1.1") is None
    assert limiter.check("1.1.1.1") is None
    assert "per hour" in (limiter.check("1.1.1.1") or "")
    assert limiter.check("2.2.2.2") is None  # global count is now 3
    assert "today" in (limiter.check("3.3.3.3") or "")
    now[0] = 3_601
    assert "today" in (limiter.check("1.1.1.1") or "")  # hourly window free, daily cap still hit
    now[0] = 86_401
    assert limiter.check("1.1.1.1") is None


def ai_client(demo_env: Path) -> TestClient:
    return TestClient(create_app(demo_env / "data", include_ai=True))


def test_demo_ai_settings_are_pinned_and_chatgpt_is_disabled(demo_env: Path) -> None:
    client = ai_client(demo_env)
    status = client.get("/api/ai/status").json()
    assert status["demo"] is True
    assert status["provider"] == "llama"
    assert status["model"] == "provider/demo-vision-model"
    assert status["llama_base_url"] == ""
    assert [provider["id"] for provider in status["providers"]] == ["llama"]

    headers = {"Origin": "https://kcallm.test"}
    response = client.put(
        "/api/ai/settings", json={"provider": "chatgpt", "model": "x"}, headers=headers
    )
    assert response.status_code == 403
    assert client.post("/api/ai/chatgpt/connect", headers=headers).status_code == 403
    models = client.get("/api/ai/models", params={"provider": "llama"}).json()["models"]
    assert [model["id"] for model in models] == ["provider/demo-vision-model"]


def test_demo_ai_requests_are_rate_limited_per_client(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_generate(self: Any, provider: str, *args: Any, **kwargs: Any) -> str:
        calls.append(provider)
        return json.dumps({"items": [], "warnings": [], "unparsed_text": "", "clarifications": []})

    monkeypatch.setattr("kcallm.ai.providers.Providers.generate", fake_generate)
    client = ai_client(demo_env)
    headers = {"Origin": "https://kcallm.test", "X-Forwarded-For": "203.0.113.7"}
    statuses = [
        client.post("/api/ai/interpret", json={"text": "1 banana"}, headers=headers).status_code
        for _ in range(3)
    ]
    assert 429 not in statuses[:2]
    assert statuses[2] == 429
    assert len(calls) == 2  # the limiter refuses before the provider is called
    other = {"Origin": "https://kcallm.test", "X-Forwarded-For": "198.51.100.9"}
    response = client.post(
        "/api/ai/interpret",
        json={"text": "1 banana", "provider": "chatgpt", "model": "gpt-anything"},
        headers=other,
    )
    assert response.status_code != 429
    assert calls == ["llama", "llama", "llama"]  # a requested provider/model is ignored


def test_demo_requires_a_model(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KCALLM_DEMO_MODEL", "")
    with pytest.raises(ValueError, match="KCALLM_DEMO_MODEL"):
        create_app(demo_env / "data", include_ai=True)
