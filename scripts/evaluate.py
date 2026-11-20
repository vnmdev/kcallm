"""Evaluate interpretation without writing to a food catalogue or diary.

Direct mode uses an isolated provider configuration and synthetic catalogue for llama.cpp.
HTTP mode uses an already running app and its approved foods; it is required for ChatGPT
so another process cannot race the running app's rotating OAuth credentials.
"""

import argparse
import asyncio
import base64
import json
import mimetypes
import os
import statistics
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from kcallm.ai.common import ProviderError
from kcallm.ai.router import AIService
from kcallm.ai.schemas import AIInput

ROOT = Path(__file__).resolve().parents[1]


def amount_key(quantity: str | None, unit: str | None) -> tuple[str | None, str | None]:
    """Compare equivalent metric units, without using binary floating point."""
    if quantity is None:
        return None, unit
    number = Decimal(quantity)
    if unit == "kg":
        number, unit = number * 1000, "g"
    elif unit == "l":
        number, unit = number * 1000, "ml"
    return str(number.normalize()), unit


def score_case(
    case: dict[str, Any], result: dict[str, Any], foods: list[dict[str, Any]]
) -> dict[str, bool]:
    by_id = {food["id"]: food for food in foods}
    actual = result.get("items", [])
    expected = case["expected"]
    outcome_ok = result.get("outcome") in expected["outcomes"]
    if "items" not in expected:
        # Clarification/no-match tests must not contain a fully specified matched proposal.
        safe = all(row.get("status") != "matched" or row.get("quantity") is None for row in actual)
        return {"outcome_correct": outcome_ok, "items_correct": safe}
    wanted = []
    for row in expected["items"]:
        quantity, unit = amount_key(row["quantity"], row["unit"])
        wanted.append((row["food_name"], quantity, unit))
    observed = []
    for row in actual:
        food = by_id.get(row.get("food_id"))
        if row.get("status") != "matched" or food is None:
            observed.append(("<unresolved>", None, None))
            continue
        quantity, unit = amount_key(row.get("quantity"), row.get("unit"))
        observed.append((food["name"], quantity, unit))
    return {
        "outcome_correct": outcome_ok,
        "items_correct": sorted(wanted, key=str) == sorted(observed, key=str),
    }


async def evaluate(args: argparse.Namespace) -> None:
    cases = json.loads(args.cases.read_text())
    cases = [case for case in cases if args.split == "all" or case["split"] == args.split]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("No cases selected.")
    if args.provider == "chatgpt" and not args.app_url:
        raise SystemExit("ChatGPT evaluations require --app-url for the running signed-in app.")
    if args.endpoint:
        os.environ["KCALLM_LLAMA_BASE_URL"] = args.endpoint

    async with httpx.AsyncClient(timeout=180, follow_redirects=False) as http:
        with tempfile.TemporaryDirectory(prefix="kcallm-evaluation-") as private:
            if args.app_url:
                response = await http.get(f"{args.app_url.rstrip('/')}/api/foods")
                response.raise_for_status()
                foods = response.json()
            else:
                foods = json.loads((ROOT / "examples/catalogue.json").read_text())
                for index, food in enumerate(foods):
                    food.update(id=f"example-{index + 1}", revision=1, archived=False)
            required = {
                row["food_name"] for case in cases for row in case["expected"].get("items", [])
            }
            if required - {food["name"] for food in foods}:
                raise SystemExit(
                    "The selected cases require matching approved foods. Use an isolated "
                    "evaluation app with examples/catalogue.json, or provide a labelled case "
                    "file for your own catalogue. No food data has been changed."
                )
            service = AIService(lambda: foods, Path(private)) if not args.app_url else None
            records = []
            try:
                for case in cases:
                    payload: dict[str, Any] = {
                        "text": case["text"],
                        "provider": args.provider,
                        "model": args.model,
                    }
                    if case.get("image_path"):
                        photo_path = (args.cases.parent / case["image_path"]).resolve()
                        mime = mimetypes.guess_type(photo_path.name)[0]
                        if mime not in {"image/jpeg", "image/png", "image/webp"}:
                            raise SystemExit("Evaluation images must be JPEG, PNG or WebP.")
                        payload["image_data_url"] = f"data:{mime};base64," + base64.b64encode(
                            photo_path.read_bytes()
                        ).decode("ascii")
                    start = time.monotonic()
                    record: dict[str, Any] = {
                        "id": case["id"],
                        "split": case["split"],
                        "has_photo": bool(case.get("image_path")),
                    }
                    try:
                        if service is not None:
                            result = await service.interpret(AIInput(**payload))
                        else:
                            response = await http.post(
                                f"{args.app_url.rstrip('/')}/api/ai/interpret", json=payload
                            )
                            response.raise_for_status()
                            result = response.json()
                        scores = score_case(case, result, foods)
                        record.update(valid_response=True, **scores, passed=all(scores.values()))
                        record["proposal"] = result
                    except ProviderError as exc:
                        record.update(valid_response=False, passed=False, error=exc.code)
                    except httpx.HTTPError as exc:
                        record.update(valid_response=False, passed=False, error=type(exc).__name__)
                    record["seconds"] = round(time.monotonic() - start, 3)
                    records.append(record)
                    print(
                        f"{case['id']}: {'pass' if record['passed'] else 'FAIL'} "
                        f"({record['seconds']}s)",
                        flush=True,
                    )
            finally:
                if service is not None:
                    await service.close()
    times = [row["seconds"] for row in records]
    report = {
        "provider": args.provider,
        "model": args.model,
        "phase": args.phase,
        "cases": len(records),
        "passed": sum(row["passed"] for row in records),
        "valid_responses": sum(row["valid_response"] for row in records),
        "first_request_seconds": times[0],
        "median_seconds": statistics.median(times),
        "max_seconds": max(times),
        "photo_cases": sum(row["has_photo"] for row in records),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(f"{report['passed']}/{len(records)} passed; report: {args.output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["llama", "chatgpt"], default="llama")
    parser.add_argument("--model", default="qwen-3.8-27b-vision-48k")
    parser.add_argument("--endpoint", help="llama.cpp base URL; direct mode only")
    parser.add_argument("--app-url", help="Running app URL, required for ChatGPT")
    parser.add_argument("--cases", type=Path, default=ROOT / "examples/evaluation.json")
    parser.add_argument("--split", choices=["development", "heldout", "all"], default="heldout")
    parser.add_argument("--phase", choices=["cold", "warm", "mixed"], default="mixed")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path, default=ROOT / ".private/evaluation.json")
    asyncio.run(evaluate(parser.parse_args()))
