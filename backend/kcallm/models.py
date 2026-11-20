"""Validated API records. Decimal strings are the boundary, never binary floats."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    field_validator,
    model_validator,
)

NUTRIENT_NAMES = ("kcal", "protein", "carbohydrates", "fat", "saturates", "sugars", "fibre", "salt")
METRIC_UNITS = {
    "g": ("mass", Decimal(1)),
    "kg": ("mass", Decimal(1000)),
    "ml": ("volume", Decimal(1)),
    "l": ("volume", Decimal(1000)),
}


def decimal_string(value: Decimal) -> str:
    """Canonical plain notation, preserving all meaningful decimal places."""
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def parse_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str) and len(value) <= 128:
        if not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value):
            raise ValueError("Use a plain decimal string, such as '180' or '0.5'.")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Invalid decimal.") from exc
    else:
        raise ValueError("Decimal values must be strings, not JSON numbers.")
    if not parsed.is_finite() or parsed.copy_abs() > Decimal("1e36"):
        raise ValueError("Decimal must be finite and within the supported range.")
    if len(decimal_string(parsed)) > 128:
        raise ValueError("Decimal precision exceeds the supported range.")
    return parsed


DecimalValue = Annotated[
    Decimal, BeforeValidator(parse_decimal), PlainSerializer(decimal_string, return_type=str)
]
PositiveDecimal = Annotated[DecimalValue, Field(gt=0, le=Decimal("1e9"))]
NonnegativeDecimal = Annotated[DecimalValue, Field(ge=0)]
Identifier = Annotated[str, Field(min_length=1, max_length=100)]
Revision = Annotated[int, Field(ge=1, strict=True)]


def clean_unit(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Unit must be a string.")
    unit = value.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9 _-]{0,31}", unit):
        raise ValueError("Use a short unit name, such as g, ml, pack or piece.")
    return unit


Unit = Annotated[str, BeforeValidator(clean_unit)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Nutrients(Record):
    kcal: NonnegativeDecimal | None = None
    protein: NonnegativeDecimal | None = None
    carbohydrates: NonnegativeDecimal | None = None
    fat: NonnegativeDecimal | None = None
    saturates: NonnegativeDecimal | None = None
    sugars: NonnegativeDecimal | None = None
    fibre: NonnegativeDecimal | None = None
    salt: NonnegativeDecimal | None = None


class Conversion(Record):
    unit: Unit
    amount: PositiveDecimal
    basis_amount: PositiveDecimal


class Serving(Record):
    quantity: PositiveDecimal
    unit: Unit


def unit_group(unit: str) -> str:
    return f"metric:{METRIC_UNITS[unit][0]}" if unit in METRIC_UNITS else f"named:{unit}"


class FoodInput(Record):
    name: str = Field(min_length=1, max_length=200)
    brand: str = Field(default="", max_length=100)
    category: str = Field(default="", max_length=100)
    preparation: str = Field(default="", max_length=300)
    aliases: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=30
    )
    basis_amount: PositiveDecimal
    basis_unit: Unit
    nutrients: Nutrients
    conversions: list[Conversion] = Field(default_factory=list, max_length=30)
    usual_serving: Serving | None = None

    @model_validator(mode="after")
    def validate_food(self) -> FoodInput:
        if self.nutrients.kcal is None:
            raise ValueError("Kcal is required before a food can enter the catalogue.")
        groups = {unit_group(self.basis_unit)}
        for conversion in self.conversions:
            group = unit_group(conversion.unit)
            if group in groups:
                raise ValueError(
                    "Conversions must not duplicate a unit or an implicit metric scale."
                )
            groups.add(group)
        if self.usual_serving and unit_group(self.usual_serving.unit) not in groups:
            raise ValueError("The usual serving needs a saved conversion to the nutrition basis.")
        if len({alias.casefold() for alias in self.aliases}) != len(self.aliases):
            raise ValueError("Aliases must be unique.")
        return self


class Food(FoodInput):
    id: Identifier
    revision: Revision
    archived: StrictBool = False


class FoodUpdate(FoodInput):
    revision: Revision


class LineInput(Record):
    food_id: Identifier
    quantity: PositiveDecimal | None = None
    unit: Unit | None = None
    entry_id: Identifier | None = None


class ReviewedLine(Record):
    food_id: Identifier
    food_revision: Revision
    quantity: PositiveDecimal
    unit: Unit
    entry_id: Identifier | None = None
    entry_revision: Revision | None = None


class PreviewLine(ReviewedLine):
    food_name: str
    brand: str
    nutrients: Nutrients


class PreviewRequest(Record):
    date: date
    items: list[LineInput] = Field(min_length=1, max_length=100)


class Preview(Record):
    date: date
    items: list[PreviewLine]
    totals: Nutrients
    incomplete: list[str]


class ConfirmRequest(Record):
    request_id: UUID
    date: date
    items: list[ReviewedLine] = Field(min_length=1, max_length=100)


class Entry(Record):
    id: Identifier
    revision: Revision
    batch_id: Identifier
    date: date
    food_id: Identifier
    food_revision: Revision
    food_name: str
    brand: str
    quantity: PositiveDecimal
    unit: Unit
    nutrients: Nutrients
    created_at: AwareDatetime


class StoredEntry(Entry):
    source_snapshot: Food


class EntryUpdate(Record):
    revision: Revision
    date: date
    food_id: Identifier
    food_revision: Revision
    quantity: PositiveDecimal
    unit: Unit


class RemainingTargets(Record):
    """Signed differences from optional goals, distinct from nutrition facts."""

    kcal: DecimalValue | None = None
    protein: DecimalValue | None = None
    carbohydrates: DecimalValue | None = None
    fat: DecimalValue | None = None
    saturates: DecimalValue | None = None
    sugars: DecimalValue | None = None
    fibre: DecimalValue | None = None
    salt: DecimalValue | None = None


class Day(Record):
    date: date
    entries: list[Entry]
    totals: Nutrients
    incomplete: list[str]
    # Older confirmation receipts/backups omit this derived display field.
    remaining: RemainingTargets = Field(default_factory=RemainingTargets)


class Settings(Record):
    revision: Revision = 1
    timezone: str = "Europe/London"
    goals: Nutrients = Field(default_factory=Nutrients)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone, such as Europe/London.") from exc
        return value


class Receipt(Record):
    request_id: UUID
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: Day
    entry_ids: list[Identifier] = Field(min_length=1, max_length=100)
    undone: StrictBool = False


class ExportData(Record):
    schema_version: Literal[1] = 1
    exported_at: AwareDatetime
    foods: list[Food] = Field(max_length=100000)
    entries: list[StoredEntry] = Field(max_length=1000000)
    settings: Settings
    receipts: list[Receipt] = Field(default_factory=list, max_length=1000000)


class RestorePreviewRequest(Record):
    data: ExportData


class RestoreRequest(RestorePreviewRequest):
    replace: Literal[True]
