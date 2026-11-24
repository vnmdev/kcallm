"""AI contracts and validation, independent of the diary/storage implementation."""

import base64
import binascii
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .common import ProviderError

Provider = Literal["chatgpt", "llama", "openrouter"]
NUTRIENTS = ("kcal", "protein", "carbohydrates", "fat", "saturates", "sugars", "fibre", "salt")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DECIMAL_PATTERN = r"^[0-9]+(?:\.[0-9]+)?$"
DecimalString = Annotated[str, Field(pattern=DECIMAL_PATTERN)]
NUTRIENT_LABELS = {
    "kcal": "Calories",
    "protein": "Protein",
    "carbohydrates": "Carbohydrates",
    "fat": "Fat",
    "saturates": "Saturates",
    "sugars": "Sugars",
    "fibre": "Fibre",
    "salt": "Salt",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def decimal_text(value: str, *, positive: bool = False) -> str:
    if len(value) > 80 or not re.fullmatch(DECIMAL_PATTERN, value):
        raise ValueError("Use a decimal string without an exponent or sign.")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal string.") from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ValueError(
            "The amount must be positive." if positive else "Use a nonnegative amount."
        )
    return value


def validate_image(value: str) -> str:
    if len(value) > 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 64:
        raise ValueError("Images must be at most 10 MiB.")
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", value)
    if not match:
        raise ValueError("Use a PNG, JPEG or WebP base64 data URL.")
    try:
        decoded = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 image.") from exc
    if not decoded or len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError("Images must be between 1 byte and 10 MiB.")
    valid_signature = {
        "png": decoded.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpeg": decoded.startswith(b"\xff\xd8\xff"),
        "webp": decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP",
    }
    if not valid_signature[match.group(1)]:
        raise ValueError("Image bytes do not match their declared format.")
    return value


class AIInput(StrictModel):
    text: str = Field(default="", max_length=12000)
    image_data_url: str | None = None
    provider: Provider | None = None
    model: str | None = Field(default=None, max_length=200)

    @field_validator("image_data_url")
    @classmethod
    def image_is_valid(cls, value: str | None) -> str | None:
        return validate_image(value) if value is not None else None


class AISettingsInput(StrictModel):
    provider: Provider
    model: str = Field(max_length=200)
    llama_base_url: str | None = Field(default=None, max_length=1000)


class ProposalItem(StrictModel):
    status: Literal["matched", "clarification", "no_match"]
    food_id: str | None
    quantity: DecimalString | None
    unit: str | None
    question: str = Field(max_length=1200)
    candidates: list[str] = Field(max_length=12)

    @field_validator("quantity")
    @classmethod
    def positive_quantity(cls, value: str | None) -> str | None:
        return decimal_text(value, positive=True) if value is not None else None


class Interpretation(StrictModel):
    outcome: Literal["proposal", "clarification", "no_match"]
    message: str = Field(max_length=2000)
    items: list[ProposalItem] = Field(min_length=1, max_length=30)


class DraftNutrients(StrictModel):
    kcal: DecimalString | None = None
    protein: DecimalString | None = None
    carbohydrates: DecimalString | None = None
    fat: DecimalString | None = None
    saturates: DecimalString | None = None
    sugars: DecimalString | None = None
    fibre: DecimalString | None = None
    salt: DecimalString | None = None

    @field_validator(*NUTRIENTS)
    @classmethod
    def nonnegative_nutrients(cls, value: str | None) -> str | None:
        return decimal_text(value) if value is not None else None


class DraftConversion(StrictModel):
    unit: str = Field(min_length=1, max_length=40)
    amount: DecimalString
    basis_amount: DecimalString

    @field_validator("amount", "basis_amount")
    @classmethod
    def positive_amount(cls, value: str) -> str:
        return decimal_text(value, positive=True)


class DraftServing(StrictModel):
    quantity: DecimalString
    unit: str = Field(min_length=1, max_length=40)

    @field_validator("quantity")
    @classmethod
    def positive_amount(cls, value: str) -> str:
        return decimal_text(value, positive=True)


class FoodDraft(StrictModel):
    name: str | None = Field(default=None, max_length=250)
    brand: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    preparation: str | None = Field(default=None, max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    basis_amount: DecimalString | None = None
    basis_unit: str | None = Field(default=None, max_length=40)
    nutrients: DraftNutrients = Field(default_factory=DraftNutrients)
    conversions: list[DraftConversion] = Field(default_factory=list, max_length=20)
    usual_serving: DraftServing | None = None

    @field_validator("basis_amount")
    @classmethod
    def positive_amount(cls, value: str | None) -> str | None:
        return decimal_text(value, positive=True) if value is not None else None


class Extraction(StrictModel):
    draft: FoodDraft
    warnings: list[str] = Field(max_length=30)


def compact_catalogue(foods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nutritional facts and diary history are deliberately not sent for matching."""
    keys = ("id", "name", "brand", "aliases", "preparation", "basis_unit", "usual_serving")
    return [
        {**{key: food.get(key) for key in keys}, "units": sorted(allowed_units(food))}
        for food in foods
        if not food.get("archived", False)
    ]


def allowed_units(food: dict[str, Any]) -> set[str]:
    units = {str(food.get("basis_unit", ""))}
    units.update(str(item["unit"]) for item in food.get("conversions", []))
    if units.intersection({"g", "kg"}):
        units.update({"g", "kg"})
    if units.intersection({"ml", "l"}):
        units.update({"ml", "l"})
    return units - {""}


def validate_interpretation(text: str, foods: list[dict[str, Any]]) -> Interpretation:
    try:
        result = Interpretation.model_validate_json(text)
    except ValidationError as exc:
        raise ProviderError(
            "invalid_proposal", "The model returned an invalid proposal. Review it manually."
        ) from exc
    catalogue = {str(food["id"]): food for food in foods if not food.get("archived", False)}
    for item in result.items:
        if (item.food_id is not None and item.food_id not in catalogue) or any(
            candidate not in catalogue for candidate in item.candidates
        ):
            raise ProviderError("unknown_food", "The model proposed a food outside your catalogue.")
        if item.candidates:
            item.candidates = list(dict.fromkeys(item.candidates))
            item.status = "clarification"
            if not item.question:
                item.question = "Which suggested food is correct? Check the food and amount."
        if item.status == "matched" and item.food_id is None:
            raise ProviderError(
                "invalid_proposal", "A matched proposal must identify a saved food."
            )
        if item.food_id is not None:
            food = catalogue[item.food_id]
            if item.unit is not None and item.unit not in allowed_units(food):
                raise ProviderError(
                    "unsupported_unit", "The proposed unit has no saved conversion for this food."
                )
            if item.status == "matched" and (item.quantity is None or item.unit is None):
                serving = food.get("usual_serving")
                if item.quantity is None and item.unit is None and isinstance(serving, dict):
                    item.quantity = str(serving["quantity"])
                    item.unit = str(serving["unit"])
                    decimal_text(item.quantity, positive=True)
                    if item.unit not in allowed_units(food):
                        raise ProviderError(
                            "invalid_serving", "The saved serving needs correction."
                        )
                else:
                    item.status = "clarification"
                    item.question = "What quantity and unit did you eat?"
    if all(item.status == "matched" for item in result.items):
        result.outcome = "proposal"
    elif all(item.status == "no_match" for item in result.items):
        result.outcome = "no_match"
    else:
        result.outcome = "clarification"
    return result


def recover_nutrient_bounds(value: Any) -> None:
    """A label bound is useful evidence, but it is never an exact nutrient value.

    Only recognize simple inequalities in known nutrient fields. Every other
    type, numeric rule and structural constraint still goes through validation.
    """
    if not isinstance(value, dict):
        return
    draft, warnings = value.get("draft"), value.get("warnings")
    if (
        not isinstance(draft, dict)
        or not isinstance(warnings, list)
        or len(warnings) > 30
        or not all(isinstance(warning, str) for warning in warnings)
    ):
        return
    nutrients = draft.get("nutrients")
    if not isinstance(nutrients, dict):
        return
    bounds = []
    for field in NUTRIENTS:
        reading = nutrients.get(field)
        if not isinstance(reading, str) or len(reading) > 100:
            continue
        match = re.fullmatch(
            r"(?:<=|>=|<|>|≤|≥)\s*([0-9]+(?:\.[0-9]+)?)\s*(g|kcal)?",
            reading.strip(),
            flags=re.IGNORECASE,
        )
        if match is None or len(match.group(1)) > 80:
            continue
        expected_unit = "kcal" if field == "kcal" else "g"
        if match.group(2) is not None and match.group(2).lower() != expected_unit:
            continue
        nutrients[field] = None
        bounds.append(f"{NUTRIENT_LABELS[field]} {reading.strip()}")
    if bounds:
        note = (
            "Label bounds are not exact values: "
            + "; ".join(bounds)
            + ". These nutrients were left unknown. Check the source before approving."
        )
        if len(warnings) < 30:
            warnings.append(note)
        else:
            # Preserve all existing source warnings and our new explanation.
            warnings[-1] += "\n" + note


def extraction_error_message(error: ValidationError) -> str:
    """Expose static field labels only, never model text, values or arbitrary keys."""
    fields = {
        "name": "Food name",
        "brand": "Brand",
        "category": "Category",
        "preparation": "Preparation",
        "aliases": "Aliases",
        "basis_amount": "Nutrition basis amount",
        "basis_unit": "Nutrition basis unit",
        "nutrients": "Nutrients",
        "conversions": "Serving conversions",
        "usual_serving": "Usual serving",
    }
    labels = set()
    for issue in error.errors(include_input=False, include_context=False, include_url=False):
        location = issue["loc"]
        if len(location) >= 2 and location[0] == "draft" and location[1] in fields:
            if len(location) == 3 and location[1] == "nutrients":
                labels.add(NUTRIENT_LABELS.get(str(location[2]), "Nutrients"))
            else:
                labels.add(fields[str(location[1])])
        elif location == ("warnings",) or location[:1] == ("warnings",):
            labels.add("Review notes")
    message = "The extracted food draft needs correction."
    if labels:
        message += " Check: " + ", ".join(sorted(labels)) + "."
    return message + " Try reading the label again or enter the food manually."


def validate_extraction(text: str) -> Extraction:
    try:
        value = json.loads(text)
    except (ValueError, UnicodeError, RecursionError):
        raise ProviderError(
            "invalid_food_draft",
            "The model returned an unreadable food draft. "
            "Try reading the label again or enter the food manually.",
        ) from None
    recover_nutrient_bounds(value)
    try:
        return Extraction.model_validate(value)
    except ValidationError as exc:
        raise ProviderError("invalid_food_draft", extraction_error_message(exc)) from None
