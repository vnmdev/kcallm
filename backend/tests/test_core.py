"""Core acceptance checks use temporary databases and never contact a model."""

from __future__ import annotations

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from kcallm.app import create_app
from kcallm.models import ConfirmRequest

DAY = "2026-11-13"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("KCALLM_PRIVATE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("KCALLM_ALLOWED_ORIGINS", "http://localhost:5173,https://kcallm.test")
    monkeypatch.setenv("KCALLM_ALLOWED_HOSTS", "testserver")
    return TestClient(create_app(tmp_path, include_ai=False))


def food_input(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "Example potato",
        "brand": "Demo",
        "category": "Carbs",
        "preparation": "Illustrative fixture",
        "aliases": ["potato"],
        "basis_amount": "100",
        "basis_unit": "g",
        "nutrients": {"kcal": "107", "protein": "2.5", "salt": "0"},
        "conversions": [],
        "usual_serving": None,
    }
    data.update(overrides)
    return data


def add(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post("/api/foods", json=food_input(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def preview(client: TestClient, items: list[dict[str, Any]]) -> dict[str, Any]:
    response = client.post("/api/preview", json={"date": DAY, "items": items})
    assert response.status_code == 200, response.text
    return response.json()


def confirmation(items: list[dict[str, Any]]) -> dict[str, Any]:
    fields = {"food_id", "food_revision", "quantity", "unit", "entry_id", "entry_revision"}
    return {
        "request_id": str(uuid4()),
        "date": DAY,
        "items": [{key: value for key, value in item.items() if key in fields} for item in items],
    }


def log(
    client: TestClient, food: dict[str, Any], quantity: str = "180", unit: str = "g"
) -> tuple[dict[str, Any], dict[str, Any]]:
    draft = preview(client, [{"food_id": food["id"], "quantity": quantity, "unit": unit}])
    body = confirmation(draft["items"])
    response = client.post("/api/diary/confirm", json=body)
    assert response.status_code == 200, response.text
    return body, response.json()


def test_exact_arithmetic_metric_and_named_units(client: TestClient) -> None:
    potato = add(client)
    result = preview(
        client,
        [
            {"food_id": potato["id"], "quantity": "0.18", "unit": "kg"},
        ],
    )
    assert result["totals"]["kcal"] == "192.6"
    assert result["totals"]["protein"] == "4.5"
    mango = add(
        client,
        name="Example mango",
        basis_amount="1",
        basis_unit="pack",
        nutrients={"kcal": "125", "protein": "1.7"},
        conversions=[{"unit": "g", "amount": "240", "basis_amount": "1"}],
    )
    result = preview(
        client,
        [
            {"food_id": mango["id"], "quantity": "2", "unit": "pack"},
            {"food_id": mango["id"], "quantity": "0.48", "unit": "kg"},
        ],
    )
    assert result["totals"]["kcal"] == "500"
    assert result["totals"]["protein"] == "6.8"
    drink = add(client, name="Example drink", basis_unit="ml")
    assert (
        preview(client, [{"food_id": drink["id"], "quantity": "0.18", "unit": "l"}])["totals"][
            "kcal"
        ]
        == "192.6"
    )
    assert (
        client.post(
            "/api/preview",
            json={"date": DAY, "items": [{"food_id": drink["id"], "quantity": "180", "unit": "g"}]},
        ).status_code
        == 422
    )


def test_sum_before_display_rounding_and_unknown_not_zero(client: TestClient) -> None:
    item = add(
        client,
        basis_amount="1",
        basis_unit="piece",
        nutrients={"kcal": "0.333", "protein": None, "fat": "0"},
    )
    result = preview(client, [{"food_id": item["id"], "quantity": "1", "unit": "piece"}] * 3)
    assert result["totals"]["kcal"] == "0.999"
    assert result["totals"]["fat"] == "0"
    assert result["totals"]["protein"] is None
    assert "protein" in result["incomplete"]
    assert "fat" not in result["incomplete"]
    known = add(client, nutrients={"kcal": "0", "protein": "0"})
    mixed = preview(
        client,
        [
            {"food_id": item["id"], "quantity": "1", "unit": "piece"},
            {"food_id": known["id"], "quantity": "100", "unit": "g"},
        ],
    )
    assert mixed["totals"]["protein"] == "0"
    assert "protein" in mixed["incomplete"]


@pytest.mark.parametrize("bad", [1, 1.1, True, "0", "-1", "NaN", "Infinity", "1e2"])
def test_bad_quantities_never_write(client: TestClient, bad: Any) -> None:
    item = add(client)
    response = client.post(
        "/api/preview",
        json={"date": DAY, "items": [{"food_id": item["id"], "quantity": bad, "unit": "g"}]},
    )
    assert response.status_code == 422
    assert client.get(f"/api/diary?date={DAY}").json()["entries"] == []


def test_only_explicit_usual_serving_resolves_missing_portion(client: TestClient) -> None:
    item = add(client)
    assert (
        client.post(
            "/api/preview", json={"date": DAY, "items": [{"food_id": item["id"]}]}
        ).status_code
        == 422
    )
    served = add(
        client,
        name="Pack",
        conversions=[{"unit": "pack", "amount": "1", "basis_amount": "240"}],
        usual_serving={"quantity": "1", "unit": "pack"},
    )
    result = preview(client, [{"food_id": served["id"]}])
    assert result["items"][0]["quantity"] == "1"
    assert result["items"][0]["unit"] == "pack"
    assert result["totals"]["kcal"] == "256.8"
    assert (
        client.post(
            "/api/preview", json={"date": DAY, "items": [{"food_id": served["id"], "unit": "g"}]}
        ).status_code
        == 422
    )


def test_food_validation_disallows_ambiguous_or_incomplete_units(client: TestClient) -> None:
    for overrides in [
        {"nutrients": {"protein": "2"}},
        {"conversions": [{"unit": "kg", "amount": "1", "basis_amount": "2"}]},
        {"usual_serving": {"quantity": "1", "unit": "pack"}},
        {"aliases": ["potato", "POTATO"]},
        {"basis_amount": "0"},
        {"name": " "},
        {"basis_unit": 1},
        {"basis_unit": ["g"]},
        {"nutrients": {"kcal": "100", "salt": "-1"}},
    ]:
        assert client.post("/api/foods", json=food_input(**overrides)).status_code == 422
    assert client.get("/api/foods").json() == []


def test_confirmation_retries_are_idempotent_across_restart(
    client: TestClient, tmp_path: Path
) -> None:
    item = add(client)
    body, first = log(client, item)
    assert client.post("/api/diary/confirm", json=body).json() == first
    restarted = TestClient(create_app(tmp_path, include_ai=False))
    assert restarted.post("/api/diary/confirm", json=body).json() == first
    assert len(restarted.get(f"/api/diary?date={DAY}").json()["entries"]) == 1
    changed = copy.deepcopy(body)
    changed["items"][0]["quantity"] = "181"
    assert restarted.post("/api/diary/confirm", json=changed).status_code == 409
    with sqlite3.connect(tmp_path / "kcallm.sqlite3") as connection:
        row = connection.execute("SELECT typeof(quantity), quantity, data FROM entries").fetchone()
        assert row[0:2] == ("text", "180")
        assert json.loads(row[2])["nutrients"]["kcal"] == "192.6"


def test_concurrent_confirmation_has_one_commit(client: TestClient) -> None:
    item = add(client)
    draft = preview(client, [{"food_id": item["id"], "quantity": "180", "unit": "g"}])
    request = ConfirmRequest.model_validate(confirmation(draft["items"]))
    store = client.app.state.store  # type: ignore[union-attr]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: store.confirm(request), range(4)))
    assert all(result == results[0] for result in results)
    assert len(client.get(f"/api/diary?date={DAY}").json()["entries"]) == 1


def test_batch_validates_every_row_before_writing(client: TestClient) -> None:
    item = add(client)
    draft = preview(client, [{"food_id": item["id"], "quantity": "180", "unit": "g"}])
    body = confirmation(draft["items"] * 2)
    body["items"][1]["unit"] = "pack"
    assert client.post("/api/diary/confirm", json=body).status_code == 422
    assert client.get(f"/api/diary?date={DAY}").json()["entries"] == []
    body["items"][1]["unit"] = "g"
    assert client.post("/api/diary/confirm", json=body).status_code == 200
    assert len(client.get(f"/api/diary?date={DAY}").json()["entries"]) == 2


def test_stale_catalogue_preview_cannot_commit(client: TestClient) -> None:
    item = add(client)
    draft = preview(client, [{"food_id": item["id"], "quantity": "100", "unit": "g"}])
    updated = client.put(
        f"/api/foods/{item['id']}",
        json={**food_input(), "revision": item["revision"], "name": "Corrected food"},
    )
    assert updated.status_code == 200
    assert client.post("/api/diary/confirm", json=confirmation(draft["items"])).status_code == 409
    assert (
        client.put(
            f"/api/foods/{item['id']}", json={**food_input(), "revision": item["revision"]}
        ).status_code
        == 409
    )
    assert client.get(f"/api/diary?date={DAY}").json()["entries"] == []


def test_history_and_edit_use_original_source_unless_replaced(client: TestClient) -> None:
    item = add(client)
    _, day = log(client, item)
    entry = day["entries"][0]
    assert (
        client.put(
            f"/api/foods/{item['id']}",
            json={
                **food_input(nutrients={"kcal": "999", "protein": "99"}, name="New label"),
                "revision": item["revision"],
            },
        ).status_code
        == 200
    )
    assert client.get(f"/api/diary?date={DAY}").json()["totals"]["kcal"] == "192.6"
    reviewed = preview(
        client, [{"entry_id": entry["id"], "food_id": item["id"], "quantity": "200", "unit": "g"}]
    )["items"][0]
    assert reviewed["food_name"] == "Example potato"
    assert reviewed["food_revision"] == 1
    assert reviewed["entry_revision"] == 1
    assert reviewed["nutrients"]["kcal"] == "214"
    edit = {
        "revision": reviewed["entry_revision"],
        "date": DAY,
        "food_id": item["id"],
        "food_revision": reviewed["food_revision"],
        "quantity": "200",
        "unit": "g",
    }
    result = client.put(f"/api/entries/{entry['id']}", json=edit)
    assert result.status_code == 200, result.text
    assert result.json()["nutrients"]["kcal"] == "214"
    assert client.put(f"/api/entries/{entry['id']}", json=edit).status_code == 409
    replacement = add(
        client, name="Other", nutrients={"kcal": "5"}, basis_amount="1", basis_unit="piece"
    )
    result = client.put(
        f"/api/entries/{entry['id']}",
        json={
            **edit,
            "revision": 2,
            "food_id": replacement["id"],
            "food_revision": 1,
            "quantity": "2",
            "unit": "piece",
            "date": "2026-11-12",
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["nutrients"]["kcal"] == "10"
    assert client.get(f"/api/diary?date={DAY}").json()["entries"] == []
    assert client.get("/api/diary?date=2026-11-12").json()["totals"]["kcal"] == "10"


def test_archive_keeps_history_and_allows_historical_portion_edit(client: TestClient) -> None:
    item = add(client)
    _, day = log(client, item)
    entry = day["entries"][0]
    assert client.delete(f"/api/foods/{item['id']}?revision=1").status_code == 204
    assert client.get("/api/foods").json() == []
    assert client.get("/api/foods?include_archived=true").json()[0]["archived"] is True
    assert client.get(f"/api/diary?date={DAY}").json()["totals"]["kcal"] == "192.6"
    assert (
        client.post(
            "/api/preview",
            json={"date": DAY, "items": [{"food_id": item["id"], "quantity": "100", "unit": "g"}]},
        ).status_code
        == 409
    )
    assert (
        preview(
            client,
            [{"entry_id": entry["id"], "food_id": item["id"], "quantity": "100", "unit": "g"}],
        )["totals"]["kcal"]
        == "107"
    )


def test_delete_is_revision_checked_and_undo_refuses_later_edits(client: TestClient) -> None:
    item = add(client)
    draft = preview(client, [{"food_id": item["id"], "quantity": "100", "unit": "g"}] * 2)
    body = confirmation(draft["items"])
    day = client.post("/api/diary/confirm", json=body).json()
    entry = day["entries"][0]
    assert (
        client.put(
            f"/api/entries/{entry['id']}",
            json={
                "revision": 1,
                "date": DAY,
                "food_id": item["id"],
                "food_revision": 1,
                "quantity": "101",
                "unit": "g",
            },
        ).status_code
        == 200
    )
    assert client.post(f"/api/batches/{body['request_id']}/undo").status_code == 409
    assert len(client.get(f"/api/diary?date={DAY}").json()["entries"]) == 2
    assert client.delete(f"/api/entries/{entry['id']}?revision=1").status_code == 409
    assert client.delete(f"/api/entries/{entry['id']}?revision=2").status_code == 204
    assert client.post(f"/api/batches/{body['request_id']}/undo").status_code == 409


def test_undo_is_atomic_repeatable_and_retry_never_resurrects(client: TestClient) -> None:
    item = add(client)
    body, original = log(client, item)
    assert client.post(f"/api/batches/{body['request_id']}/undo").status_code == 204
    assert client.post(f"/api/batches/{body['request_id']}/undo").status_code == 204
    assert client.post("/api/diary/confirm", json=body).json() == original
    assert client.get(f"/api/diary?date={DAY}").json()["entries"] == []


def test_settings_goals_and_timezone_are_optional_and_revision_checked(client: TestClient) -> None:
    initial = client.get("/api/settings").json()
    assert initial["timezone"] == "Europe/London"
    assert all(value is None for value in initial["goals"].values())
    update = {**initial, "goals": {"kcal": "2000", "protein": "120.5"}}
    assert client.put("/api/settings", json=update).json()["revision"] == 2
    assert client.put("/api/settings", json=update).status_code == 409
    assert (
        client.put(
            "/api/settings", json={**update, "revision": 2, "timezone": "made/up"}
        ).status_code
        == 422
    )


def test_export_restore_preserves_snapshots_totals_and_idempotency(client: TestClient) -> None:
    item = add(client)
    body, original = log(client, item)
    backup = client.get("/api/export").json()
    assert backup["schema_version"] == 1
    assert backup["entries"][0]["source_snapshot"]["nutrients"]["kcal"] == "107"
    assert set(backup) == {
        "schema_version",
        "exported_at",
        "foods",
        "entries",
        "settings",
        "receipts",
    }
    assert client.post("/api/restore/preview", json={"data": backup}).json() == {
        "foods": 1,
        "entries": 1,
    }
    add(client, name="Later addition")
    assert client.post("/api/restore", json={"replace": True, "data": backup}).status_code == 200
    assert len(client.get("/api/foods").json()) == 1
    restored = client.get(f"/api/diary?date={DAY}").json()
    assert restored["totals"] == original["totals"]
    assert restored["entries"][0]["revision"] > original["entries"][0]["revision"]
    assert client.post("/api/diary/confirm", json=body).json() == original
    assert len(client.get(f"/api/diary?date={DAY}").json()["entries"]) == 1
    # An in-flight new batch reviewed before restore is now stale.
    stale = {**body, "request_id": str(uuid4())}
    assert client.post("/api/diary/confirm", json=stale).status_code == 409


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate",
        "missing_food",
        "wrong_total",
        "wrong_source",
        "number",
        "unknown_version",
        "secret",
        "bad_receipt",
        "missing_receipt",
    ],
)
def test_invalid_restore_never_partially_replaces(client: TestClient, damage: str) -> None:
    item = add(client)
    log(client, item)
    original = client.get("/api/export").json()
    broken = copy.deepcopy(original)
    if damage == "duplicate":
        broken["foods"].append(copy.deepcopy(broken["foods"][0]))
    elif damage == "missing_food":
        broken["foods"] = []
    elif damage == "wrong_total":
        broken["entries"][0]["nutrients"]["kcal"] = "1"
    elif damage == "wrong_source":
        broken["entries"][0]["source_snapshot"]["name"] = "Wrong name"
    elif damage == "number":
        broken["foods"][0]["nutrients"]["kcal"] = 107.0
    elif damage == "unknown_version":
        broken["schema_version"] = 2
    elif damage == "secret":
        broken["settings"]["api_key"] = "must-not-import"
    elif damage == "bad_receipt":
        broken["receipts"][0]["entry_ids"] = ["missing"]
    elif damage == "missing_receipt":
        broken["receipts"] = []
    for path, body in [
        ("/api/restore/preview", {"data": broken}),
        ("/api/restore", {"replace": True, "data": broken}),
    ]:
        assert client.post(path, json=body).status_code == 422
        current = client.get("/api/export").json()
        assert {k: v for k, v in current.items() if k != "exported_at"} == {
            k: v for k, v in original.items() if k != "exported_at"
        }
    assert client.post("/api/restore", json={"replace": False, "data": original}).status_code == 422


def test_origin_guard_and_private_starter_drafts(client: TestClient, tmp_path: Path) -> None:
    assert (
        client.post(
            "/api/foods", json=food_input(), headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/foods",
            json=food_input(),
            headers={"Origin": "https://kcallm.test", "Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/foods",
            json=food_input(),
            headers={"Origin": "https://kcallm.test", "Sec-Fetch-Site": "same-origin"},
        ).status_code
        == 201
    )
    assert client.get("/api/starter-drafts").json() == []
    private = tmp_path / "private"
    private.mkdir()
    draft = {"name": "Unreviewed fixture", "basis_amount": None, "basis_unit": None}
    (private / "starter-drafts.json").write_text(json.dumps([draft]))
    assert client.get("/api/starter-drafts").json() == [draft]
    assert len(client.get("/api/foods").json()) == 1
    assert client.get("/api/export").headers["cache-control"] == "no-store"


def test_extreme_portions_fail_as_validation_errors_without_writes(client: TestClient) -> None:
    item = add(client, basis_amount="0." + "0" * 100 + "1")
    response = client.post(
        "/api/preview",
        json={
            "date": DAY,
            "items": [{"food_id": item["id"], "quantity": "1000000000", "unit": "g"}],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_portion"
    body = {
        "request_id": str(uuid4()),
        "date": DAY,
        "items": [
            {"food_id": item["id"], "food_revision": 1, "quantity": "1000000000", "unit": "g"}
        ],
    }
    assert client.post("/api/diary/confirm", json=body).status_code == 422
    assert client.get(f"/api/diary?date={DAY}").json()["entries"] == []


def test_out_of_range_daily_totals_cannot_be_committed_or_imported(client: TestClient) -> None:
    item = add(client, basis_amount="1", basis_unit="piece", nutrients={"kcal": str(10**36)})
    body, _ = log(client, item, "1", "piece")
    assert (
        client.post("/api/diary/confirm", json={**body, "request_id": str(uuid4())}).status_code
        == 422
    )
    other_date = "2026-11-12"
    other = client.post(
        "/api/diary/confirm", json={**body, "request_id": str(uuid4()), "date": other_date}
    ).json()["entries"][0]
    assert (
        client.put(
            f"/api/entries/{other['id']}",
            json={
                "revision": 1,
                "date": DAY,
                "food_id": item["id"],
                "food_revision": 1,
                "quantity": "1",
                "unit": "piece",
            },
        ).status_code
        == 422
    )
    assert len(client.get(f"/api/diary?date={other_date}").json()["entries"]) == 1
    assert len(client.get(f"/api/diary?date={DAY}").json()["entries"]) == 1
    backup = client.get("/api/export").json()
    backup["entries"][1]["date"] = DAY
    assert client.post("/api/restore", json={"replace": True, "data": backup}).status_code == 422


@pytest.mark.parametrize(
    "host",
    [
        "evil.test",
        "localhost.evil.test",
        "localhost/evil",
        "evil@localhost",
        "localhost:bad",
    ],
)
def test_untrusted_host_cannot_read_private_data(client: TestClient, host: str) -> None:
    for path in ("/api/diary?date=" + DAY, "/api/export", "/api/health"):
        response = client.get(path, headers={"host": host})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "untrusted_host"


def test_host_guards_allow_configured_origin_and_explicit_hosts(client: TestClient) -> None:
    for host in ("testserver", "localhost:8000", "127.0.0.1:8000", "kcallm.test", "KCALLM.test"):
        assert client.get("/api/health", headers={"host": host}).status_code == 200
    assert (
        client.post(
            "/api/foods",
            json=food_input(),
            headers={"host": "kcallm.test", "origin": "https://kcallm.test"},
        ).status_code
        == 201
    )
    assert (
        client.get(
            "/api/export", headers=[("host", "testserver"), ("host", "evil.test")]
        ).status_code
        == 400
    )


def test_testserver_is_not_trusted_in_production_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KCALLM_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("KCALLM_ALLOWED_ORIGINS", raising=False)
    app = create_app(tmp_path, include_ai=False)
    assert TestClient(app).get("/api/health").status_code == 400
    assert TestClient(app, base_url="http://localhost").get("/api/health").status_code == 200


def test_remaining_targets_are_exact_signed_and_require_complete_totals(client: TestClient) -> None:
    assert all(
        value is None for value in client.get(f"/api/diary?date={DAY}").json()["remaining"].values()
    )
    settings = client.get("/api/settings").json()
    goals = {"kcal": "2000", "protein": "150.5", "fibre": "30", "salt": "5"}
    saved = client.put("/api/settings", json={**settings, "goals": goals}).json()
    empty = client.get(f"/api/diary?date={DAY}").json()["remaining"]
    assert {key: empty[key] for key in goals} == goals
    assert empty["fat"] is None
    item = add(client)
    _, confirmed = log(client, item)
    remaining = confirmed["remaining"]
    assert remaining["kcal"] == "1807.4"
    assert remaining["protein"] == "146"
    assert remaining["salt"] == "5"
    assert remaining["fibre"] is None
    assert client.get(f"/api/diary?date={DAY}").json()["remaining"] == remaining
    partial = add(client, name="Unknown protein", nutrients={"kcal": "0", "salt": "0"})
    _, mixed = log(client, partial, "100", "g")
    assert mixed["totals"]["protein"] == "4.5"
    assert mixed["remaining"]["protein"] is None
    assert (
        client.put(
            "/api/settings", json={**saved, "goals": {"kcal": "100", "salt": "0"}}
        ).status_code
        == 200
    )
    changed = client.get(f"/api/diary?date={DAY}").json()["remaining"]
    assert changed["kcal"] == "-92.6"
    assert changed["salt"] == "0"
    assert changed["protein"] is None


def test_backup_and_receipts_without_remaining_still_restore(client: TestClient) -> None:
    settings = client.get("/api/settings").json()
    client.put("/api/settings", json={**settings, "goals": {"kcal": "200"}})
    item = add(client)
    body, _ = log(client, item)
    backup = client.get("/api/export").json()
    del backup["receipts"][0]["result"]["remaining"]
    assert client.post("/api/restore/preview", json={"data": backup}).status_code == 200
    assert client.post("/api/restore", json={"replace": True, "data": backup}).status_code == 200
    assert client.get(f"/api/diary?date={DAY}").json()["remaining"]["kcal"] == "7.4"
    retry = client.post("/api/diary/confirm", json=body)
    assert retry.status_code == 200
    assert len(retry.json()["entries"]) == 1
    assert all(value is None for value in retry.json()["remaining"].values())
