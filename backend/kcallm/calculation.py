"""Pure nutrition arithmetic, shared by preview, persistence and restore validation."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, DecimalException, localcontext

from pydantic import ValidationError

from .models import METRIC_UNITS, NUTRIENT_NAMES, Food, Nutrients, RemainingTargets, Serving


class CalculationError(ValueError):
    pass


def metric_factor(source: str, target: str) -> Decimal | None:
    if source == target:
        return Decimal(1)
    if source in METRIC_UNITS and target in METRIC_UNITS:
        source_group, source_scale = METRIC_UNITS[source]
        target_group, target_scale = METRIC_UNITS[target]
        if source_group == target_group:
            return source_scale / target_scale
    return None


def resolve_serving(food: Food, quantity: Decimal | None, unit: str | None) -> Serving:
    """A missing amount is allowed only when a compatible usual serving was saved."""
    usual = food.usual_serving
    if quantity is None:
        if usual is None or (unit is not None and unit != usual.unit):
            raise CalculationError("Enter a quantity; no matching usual serving is saved.")
        quantity = usual.quantity
    if unit is None:
        if usual is None:
            raise CalculationError("Enter a unit; no usual serving is saved.")
        unit = usual.unit
    return Serving(quantity=quantity, unit=unit)


def calculate(food: Food, quantity: Decimal, unit: str) -> Nutrients:
    try:
        return _calculate(food, quantity, unit)
    except (DecimalException, ValidationError) as exc:
        raise CalculationError("This portion exceeds the supported numeric range.") from exc


def _calculate(food: Food, quantity: Decimal, unit: str) -> Nutrients:
    if not quantity.is_finite() or quantity <= 0:
        raise CalculationError("Quantity must be positive and finite.")
    with localcontext() as context:
        context.prec = 64
        factor = metric_factor(unit, food.basis_unit)
        if factor is not None:
            numerator = quantity * factor
            denominator = food.basis_amount
        else:
            conversion = next(
                (c for c in food.conversions if metric_factor(unit, c.unit) is not None), None
            )
            if conversion is None:
                raise CalculationError(
                    f"No saved conversion from {unit} to {food.basis_unit} for {food.name}."
                )
            conversion_factor = metric_factor(unit, conversion.unit)
            assert conversion_factor is not None
            numerator = quantity * conversion_factor * conversion.basis_amount
            denominator = conversion.amount * food.basis_amount
        values = {
            name: None
            if (value := getattr(food.nutrients, name)) is None
            else value * numerator / denominator
            for name in NUTRIENT_NAMES
        }
        return Nutrients(**values)


def totals(rows: Iterable[Nutrients]) -> tuple[Nutrients, list[str]]:
    rows = list(rows)
    result: dict[str, Decimal | None] = {}
    incomplete: list[str] = []
    with localcontext() as context:
        context.prec = 64
        for name in NUTRIENT_NAMES:
            values = [getattr(row, name) for row in rows]
            known = [value for value in values if value is not None]
            # An empty day has zero intake. An unknown-only nutrient is unknown.
            result[name] = sum(known, Decimal(0)) if known or not rows else None
            if any(value is None for value in values):
                incomplete.append(name)
    try:
        return Nutrients(**result), incomplete
    except (DecimalException, ValidationError) as exc:
        raise CalculationError("These totals exceed the supported numeric range.") from exc


def remaining_targets(
    goals: Nutrients, consumed: Nutrients, incomplete: list[str]
) -> RemainingTargets:
    values: dict[str, Decimal | None] = {}
    with localcontext() as context:
        context.prec = 64
        for name in NUTRIENT_NAMES:
            target, total = getattr(goals, name), getattr(consumed, name)
            values[name] = (
                target - total
                if target is not None and total is not None and name not in incomplete
                else None
            )
    return RemainingTargets(**values)
