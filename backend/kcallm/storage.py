"""SQLite persistence with explicit transactions and immutable nutrition snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .calculation import CalculationError, calculate, remaining_targets, resolve_serving, totals
from .models import (
    ConfirmRequest,
    Day,
    Entry,
    EntryUpdate,
    ExportData,
    Food,
    FoodInput,
    FoodUpdate,
    Nutrients,
    Preview,
    PreviewLine,
    PreviewRequest,
    Receipt,
    Settings,
    StoredEntry,
    decimal_string,
)


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def check_revision(actual: int, reviewed: int, subject: str) -> None:
    if actual != reviewed:
        raise DomainError(
            "stale_revision", f"{subject} changed. Refresh and review it before saving.", 409
        )


def public_entry(entry: StoredEntry) -> Entry:
    return Entry.model_validate(entry.model_dump(exclude={"source_snapshot"}))


class Store:
    """Each operation uses its own connection; write batches acquire one SQLite lock."""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = data_dir / "kcallm.sqlite3"
        with self.connection(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("This database version needs a newer KcaLLM release.")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS foods (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    batch_id TEXT NOT NULL, date TEXT NOT NULL,
                    food_id TEXT NOT NULL REFERENCES foods(id),
                    quantity TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS entries_day ON entries(date);
                CREATE INDEX IF NOT EXISTS entries_batch ON entries(batch_id);
                CREATE TABLE IF NOT EXISTS receipts (
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    response TEXT NOT NULL, entry_ids TEXT NOT NULL,
                    undone INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1), data TEXT NOT NULL
                );
                PRAGMA user_version = 1;
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO settings(singleton, data) VALUES (1, ?)",
                (Settings().model_dump_json(),),
            )
        self.path.chmod(0o600)

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _food(connection: sqlite3.Connection, food_id: str) -> Food:
        row = connection.execute("SELECT data FROM foods WHERE id = ?", (food_id,)).fetchone()
        if row is None:
            raise DomainError("food_not_found", "This food is no longer in the catalogue.", 404)
        return Food.model_validate_json(row["data"])

    @staticmethod
    def _entry(connection: sqlite3.Connection, entry_id: str) -> StoredEntry:
        row = connection.execute("SELECT data FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise DomainError("entry_not_found", "This diary entry no longer exists.", 404)
        return StoredEntry.model_validate_json(row["data"])

    @staticmethod
    def _put_food(connection: sqlite3.Connection, food: Food) -> None:
        connection.execute(
            "INSERT INTO foods(id, revision, archived, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, "
            "archived=excluded.archived, data=excluded.data",
            (food.id, food.revision, int(food.archived), food.model_dump_json()),
        )

    @staticmethod
    def _put_entry(connection: sqlite3.Connection, entry: StoredEntry) -> None:
        connection.execute(
            "INSERT INTO entries(id, revision, batch_id, date, food_id, quantity, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "revision=excluded.revision, batch_id=excluded.batch_id, date=excluded.date, "
            "food_id=excluded.food_id, quantity=excluded.quantity, data=excluded.data",
            (
                entry.id,
                entry.revision,
                entry.batch_id,
                entry.date.isoformat(),
                entry.food_id,
                decimal_string(entry.quantity),
                entry.model_dump_json(),
            ),
        )

    @staticmethod
    def _put_receipt(connection: sqlite3.Connection, receipt: Receipt) -> None:
        connection.execute(
            "INSERT INTO receipts(request_id, fingerprint, response, entry_ids, undone) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(receipt.request_id),
                receipt.fingerprint,
                receipt.result.model_dump_json(),
                json.dumps(receipt.entry_ids),
                int(receipt.undone),
            ),
        )

    def foods(self, *, include_archived: bool = False) -> list[Food]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT data FROM foods WHERE (? OR archived = 0) ORDER BY rowid",
                (include_archived,),
            ).fetchall()
            return [Food.model_validate_json(row["data"]) for row in rows]

    def add_food(self, data: FoodInput) -> Food:
        food = Food(**data.model_dump(), id=str(uuid4()), revision=1)
        with self.connection(write=True) as connection:
            self._put_food(connection, food)
        return food

    def update_food(self, food_id: str, data: FoodUpdate) -> Food:
        with self.connection(write=True) as connection:
            previous = self._food(connection, food_id)
            check_revision(previous.revision, data.revision, "This food")
            if previous.archived:
                raise DomainError("food_archived", "Archived foods cannot be edited.", 409)
            food = Food(
                **data.model_dump(exclude={"revision"}),
                id=food_id,
                revision=previous.revision + 1,
            )
            self._put_food(connection, food)
            return food

    def archive_food(self, food_id: str, revision: int) -> None:
        with self.connection(write=True) as connection:
            food = self._food(connection, food_id)
            check_revision(food.revision, revision, "This food")
            self._put_food(
                connection,
                food.model_copy(update={"revision": food.revision + 1, "archived": True}),
            )

    def _source(
        self, connection: sqlite3.Connection, food_id: str, entry_id: str | None = None
    ) -> tuple[Food, StoredEntry | None]:
        entry = self._entry(connection, entry_id) if entry_id else None
        if entry is not None and entry.food_id == food_id:
            return entry.source_snapshot, entry
        food = self._food(connection, food_id)
        if food.archived:
            raise DomainError("food_archived", "Choose an active food from the catalogue.", 409)
        return food, entry

    def preview(self, request: PreviewRequest) -> Preview:
        items: list[PreviewLine] = []
        with self.connection() as connection:
            for line in request.items:
                source, entry = self._source(connection, line.food_id, line.entry_id)
                serving = resolve_serving(source, line.quantity, line.unit)
                items.append(
                    PreviewLine(
                        food_id=source.id,
                        food_revision=source.revision,
                        quantity=serving.quantity,
                        unit=serving.unit,
                        entry_id=entry.id if entry else None,
                        entry_revision=entry.revision if entry else None,
                        food_name=source.name,
                        brand=source.brand,
                        nutrients=calculate(source, serving.quantity, serving.unit),
                    )
                )
        summed, incomplete = totals(item.nutrients for item in items)
        return Preview(date=request.date, items=items, totals=summed, incomplete=incomplete)

    def _day(self, connection: sqlite3.Connection, day: date) -> Day:
        rows = connection.execute(
            "SELECT data FROM entries WHERE date = ? ORDER BY rowid", (day.isoformat(),)
        ).fetchall()
        entries = [public_entry(StoredEntry.model_validate_json(row["data"])) for row in rows]
        summed, incomplete = totals(entry.nutrients for entry in entries)
        return Day(
            date=day,
            entries=entries,
            totals=summed,
            incomplete=incomplete,
            remaining=remaining_targets(self._settings(connection).goals, summed, incomplete),
        )

    def day(self, day: date) -> Day:
        with self.connection() as connection:
            return self._day(connection, day)

    def confirm(self, request: ConfirmRequest) -> Day:
        body = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(body.encode()).hexdigest()
        request_id = str(request.request_id)
        with self.connection(write=True) as connection:
            prior = connection.execute(
                "SELECT fingerprint, response FROM receipts WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior is not None:
                if prior["fingerprint"] != fingerprint:
                    raise DomainError(
                        "request_id_reused",
                        "This confirmation ID already belongs to another batch.",
                        409,
                    )
                return Day.model_validate_json(prior["response"])
            entries: list[StoredEntry] = []
            for line in request.items:
                if line.entry_id is not None or line.entry_revision is not None:
                    raise DomainError("edit_endpoint_required", "Edit saved entries individually.")
                source, _ = self._source(connection, line.food_id)
                check_revision(source.revision, line.food_revision, "This preview's food")
                entries.append(
                    StoredEntry(
                        id=str(uuid4()),
                        revision=1,
                        batch_id=request_id,
                        date=request.date,
                        food_id=source.id,
                        food_revision=source.revision,
                        food_name=source.name,
                        brand=source.brand,
                        quantity=line.quantity,
                        unit=line.unit,
                        nutrients=calculate(source, line.quantity, line.unit),
                        created_at=datetime.now(UTC),
                        source_snapshot=source,
                    )
                )
            for entry in entries:
                self._put_entry(connection, entry)
            result = self._day(connection, request.date)
            self._put_receipt(
                connection,
                Receipt(
                    request_id=request.request_id,
                    fingerprint=fingerprint,
                    result=result,
                    entry_ids=[entry.id for entry in entries],
                ),
            )
            return result

    def update_entry(self, entry_id: str, data: EntryUpdate) -> Entry:
        with self.connection(write=True) as connection:
            source, previous = self._source(connection, data.food_id, entry_id)
            assert previous is not None
            check_revision(previous.revision, data.revision, "This diary entry")
            check_revision(source.revision, data.food_revision, "This preview's food")
            updated = StoredEntry(
                id=previous.id,
                revision=previous.revision + 1,
                batch_id=previous.batch_id,
                date=data.date,
                food_id=source.id,
                food_revision=source.revision,
                food_name=source.name,
                brand=source.brand,
                quantity=data.quantity,
                unit=data.unit,
                nutrients=calculate(source, data.quantity, data.unit),
                created_at=previous.created_at,
                source_snapshot=source,
            )
            self._put_entry(connection, updated)
            # Reject an edit atomically if its destination day's total cannot be represented.
            self._day(connection, updated.date)
            return public_entry(updated)

    def delete_entry(self, entry_id: str, revision: int) -> None:
        with self.connection(write=True) as connection:
            previous = self._entry(connection, entry_id)
            check_revision(previous.revision, revision, "This diary entry")
            connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

    def undo(self, request_id: str) -> None:
        with self.connection(write=True) as connection:
            receipt = connection.execute(
                "SELECT entry_ids, undone FROM receipts WHERE request_id = ?", (request_id,)
            ).fetchone()
            if receipt is None:
                raise DomainError("batch_not_found", "This confirmation batch does not exist.", 404)
            if receipt["undone"]:
                return
            expected_ids = set(json.loads(receipt["entry_ids"]))
            rows = connection.execute(
                "SELECT id, revision FROM entries WHERE batch_id = ?", (request_id,)
            ).fetchall()
            if {row["id"] for row in rows} != expected_ids or any(
                row["revision"] != 1 for row in rows
            ):
                raise DomainError(
                    "batch_changed",
                    "This batch has been edited or deleted; undo would lose changes.",
                    409,
                )
            connection.execute("DELETE FROM entries WHERE batch_id = ?", (request_id,))
            connection.execute("UPDATE receipts SET undone = 1 WHERE request_id = ?", (request_id,))

    @staticmethod
    def _settings(connection: sqlite3.Connection) -> Settings:
        row = connection.execute("SELECT data FROM settings WHERE singleton = 1").fetchone()
        return Settings.model_validate_json(row["data"])

    def settings(self) -> Settings:
        with self.connection() as connection:
            return self._settings(connection)

    def update_settings(self, data: Settings) -> Settings:
        with self.connection(write=True) as connection:
            previous = self._settings(connection)
            check_revision(previous.revision, data.revision, "Settings")
            updated = data.model_copy(update={"revision": previous.revision + 1})
            connection.execute(
                "UPDATE settings SET data = ? WHERE singleton = 1", (updated.model_dump_json(),)
            )
            return updated

    def export(self) -> ExportData:
        with self.connection() as connection:
            foods = [
                Food.model_validate_json(row["data"])
                for row in connection.execute("SELECT data FROM foods ORDER BY rowid")
            ]
            entries = [
                StoredEntry.model_validate_json(row["data"])
                for row in connection.execute("SELECT data FROM entries ORDER BY rowid")
            ]
            receipts = [
                Receipt(
                    request_id=row["request_id"],
                    fingerprint=row["fingerprint"],
                    result=Day.model_validate_json(row["response"]),
                    entry_ids=json.loads(row["entry_ids"]),
                    undone=bool(row["undone"]),
                )
                for row in connection.execute("SELECT * FROM receipts ORDER BY rowid")
            ]
            return ExportData(
                exported_at=datetime.now(UTC),
                foods=foods,
                entries=entries,
                settings=self._settings(connection),
                receipts=receipts,
            )

    @staticmethod
    def validate_restore(data: ExportData) -> dict[str, int]:
        def unique(values: list[str], label: str) -> None:
            if len(values) != len(set(values)):
                raise DomainError("invalid_backup", f"The backup contains duplicate {label}.")

        unique([food.id for food in data.foods], "food IDs")
        unique([entry.id for entry in data.entries], "entry IDs")
        unique([str(receipt.request_id) for receipt in data.receipts], "confirmation IDs")
        food_ids = {food.id for food in data.foods}
        current_entries = {entry.id: entry for entry in data.entries}
        receipts = {str(receipt.request_id): receipt for receipt in data.receipts}
        for entry in data.entries:
            source = entry.source_snapshot
            if entry.batch_id not in receipts or entry.id not in receipts[entry.batch_id].entry_ids:
                raise DomainError("invalid_backup", "An entry is missing its confirmation receipt.")
            if entry.food_id not in food_ids or source.id != entry.food_id:
                raise DomainError("invalid_backup", "An entry references a missing or wrong food.")
            if (source.revision, source.name, source.brand) != (
                entry.food_revision,
                entry.food_name,
                entry.brand,
            ):
                raise DomainError(
                    "invalid_backup", "An entry disagrees with its historical source."
                )
            try:
                expected = calculate(source, entry.quantity, entry.unit)
            except CalculationError as exc:
                raise DomainError("invalid_backup", str(exc)) from exc
            if expected != entry.nutrients:
                raise DomainError(
                    "invalid_backup", "An entry's nutrition does not match its source."
                )
        for receipt in data.receipts:
            unique(receipt.entry_ids, "batch entry IDs")
            unique([entry.id for entry in receipt.result.entries], "confirmation result entry IDs")
            result_entries = {entry.id: entry for entry in receipt.result.entries}
            if any(entry_id not in result_entries for entry_id in receipt.entry_ids):
                raise DomainError(
                    "invalid_backup", "A confirmation is missing its original entries."
                )
            if any(
                result_entries[entry_id].batch_id != str(receipt.request_id)
                for entry_id in receipt.entry_ids
            ):
                raise DomainError("invalid_backup", "A confirmation refers to the wrong batch.")
            if receipt.undone and any(
                entry_id in current_entries for entry_id in receipt.entry_ids
            ):
                raise DomainError(
                    "invalid_backup", "An undone batch still contains active entries."
                )
            summed, incomplete = totals(entry.nutrients for entry in receipt.result.entries)
            if summed != receipt.result.totals or incomplete != receipt.result.incomplete:
                raise DomainError("invalid_backup", "A confirmation contains inconsistent totals.")
            if any(entry.date != receipt.result.date for entry in receipt.result.entries):
                raise DomainError("invalid_backup", "A confirmation mixes diary dates.")
        for day in {entry.date for entry in data.entries}:
            totals(entry.nutrients for entry in data.entries if entry.date == day)
        return {"foods": len(data.foods), "entries": len(data.entries)}

    def restore(self, data: ExportData) -> dict[str, int]:
        counts = self.validate_restore(data)
        with self.connection(write=True) as connection:
            food_revisions = {
                row["id"]: row["revision"]
                for row in connection.execute("SELECT id, revision FROM foods")
            }
            entry_revisions = {
                row["id"]: row["revision"]
                for row in connection.execute("SELECT id, revision FROM entries")
            }
            settings_revision = self._settings(connection).revision
            connection.execute("DELETE FROM entries")
            connection.execute("DELETE FROM foods")
            connection.execute("DELETE FROM receipts")
            # In-flight previews/edits from before replacement must become stale.
            for food in data.foods:
                revision = max(food.revision, food_revisions.get(food.id, 0)) + 1
                self._put_food(connection, food.model_copy(update={"revision": revision}))
            for entry in data.entries:
                revision = max(entry.revision, entry_revisions.get(entry.id, 0)) + 1
                self._put_entry(connection, entry.model_copy(update={"revision": revision}))
            for receipt in data.receipts:
                self._put_receipt(connection, receipt)
            settings = data.settings.model_copy(
                update={"revision": max(data.settings.revision, settings_revision) + 1}
            )
            connection.execute(
                "UPDATE settings SET data = ? WHERE singleton = 1", (settings.model_dump_json(),)
            )
        return counts

    def reset_to_seed(self, foods: list[FoodInput], goals: Nutrients) -> None:
        """Replace all data with a fresh catalogue. Used only by the public demo."""
        with self.connection(write=True) as connection:
            settings_revision = self._settings(connection).revision
            connection.execute("DELETE FROM entries")
            connection.execute("DELETE FROM foods")
            connection.execute("DELETE FROM receipts")
            for data in foods:
                self._put_food(connection, Food(**data.model_dump(), id=str(uuid4()), revision=1))
            settings = Settings(revision=settings_revision + 1, goals=goals)
            connection.execute(
                "UPDATE settings SET data = ? WHERE singleton = 1", (settings.model_dump_json(),)
            )

    def catalogue_json(self) -> list[dict[str, Any]]:
        return [food.model_dump(mode="json") for food in self.foods()]
