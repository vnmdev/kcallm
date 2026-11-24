"""Provider contracts tested with synthetic foods and mocked network responses."""

import asyncio
import base64
import json
import stat
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from kcallm.ai.auth import AUTH_ORIGIN, PLAN_SCOPE, RESOURCE, ChatGPTAuth
from kcallm.ai.common import ProviderError, read_private_json, write_private_json
from kcallm.ai.providers import (
    MAX_PROVIDER_OUTPUT,
    Providers,
    llama_json_schema,
    normalize_base_url,
    strict_json_schema,
)
from kcallm.ai.router import AIService
from kcallm.ai.schemas import (
    DECIMAL_PATTERN,
    MAX_IMAGE_BYTES,
    AIInput,
    AISettingsInput,
    Extraction,
    Interpretation,
    compact_catalogue,
    validate_extraction,
    validate_interpretation,
)
from kcallm.ai.transport import MAX_JSON_BYTES, READ_CHUNK_BYTES, request_json
from pydantic import ValidationError


@pytest.fixture
def foods() -> list[dict[str, Any]]:
    return [
        {
            "id": "demo-mango",
            "name": "Mango",
            "brand": "Demo",
            "aliases": ["mango packs"],
            "preparation": "ready to eat",
            "basis_unit": "g",
            "basis_amount": "100",
            "nutrients": {"kcal": "50", "protein": "1"},
            "conversions": [{"unit": "pack", "amount": "1", "basis_amount": "250"}],
            "usual_serving": None,
        }
    ]


def proposal(**changes: Any) -> str:
    item = {
        "status": "matched",
        "food_id": "demo-mango",
        "quantity": "2",
        "unit": "pack",
        "question": "",
        "candidates": [],
        **changes,
    }
    return json.dumps({"outcome": "proposal", "message": "Review this portion.", "items": [item]})


def image_url() -> str:
    # Synthetic PNG signature suffices for transport/format validation; no personal image fixture.
    return "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()


def connected_record() -> dict[str, Any]:
    return {
        "client_id": "oaiapp_test",
        "subject": "test-subject",
        "scopes": [PLAN_SCOPE],
        "credentials": {
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "expires_at": time.time() + 3600,
        },
    }


class FakeOAuth:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(
            self.key.public_key(), as_dict=True
        )
        self.jwk["kid"] = "test-key"
        self.nonce = ""
        self.scopes = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
        self.token_calls = 0
        self.forms: list[dict[str, list[str]]] = []
        self.refresh_error: str | None = None
        self.jwks_unavailable = False
        self.revocation_fails = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "jwks_uri": f"{AUTH_ORIGIN}/jwks",
                    "revocation_endpoint": f"{AUTH_ORIGIN}/revoke",
                },
            )
        if request.url.path == "/jwks":
            if self.jwks_unavailable:
                return httpx.Response(503)
            return httpx.Response(200, json={"keys": [self.jwk]})
        if request.url.path == "/revoke":
            return httpx.Response(503 if self.revocation_fails else 200)
        if request.url.path.endswith("/oauth/token"):
            self.token_calls += 1
            form = parse_qs(request.content.decode())
            self.forms.append(form)
            refresh = form["grant_type"] == ["refresh_token"]
            if refresh and self.refresh_error:
                return httpx.Response(400, json={"error": self.refresh_error})
            identity = jwt.encode(
                {
                    "iss": AUTH_ORIGIN,
                    "sub": "test-subject",
                    "aud": form["client_id"][0],
                    "exp": time.time() + 3600,
                    "nonce": self.nonce,
                    "email": "demo@example.test",
                },
                self.key,
                algorithm="RS256",
                headers={"kid": "test-key"},
            )
            return httpx.Response(
                200,
                json={
                    "access_token": f"access-{self.token_calls}",
                    "refresh_token": f"refresh-{self.token_calls}",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": self.scopes,
                    "id_token": identity,
                },
            )
        raise AssertionError(f"Unexpected mocked path: {request.url.path}")


@pytest.mark.asyncio
async def test_oauth_state_pkce_registration_and_private_storage(tmp_path: Path) -> None:
    fake = FakeOAuth()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        auth = ChatGPTAuth(tmp_path / "private", http, callback_port=0)
        result = await auth.connect()
        query = parse_qs(urlsplit(result["authorization_url"]).query)
        assert query["client_id"] == ["dynamic_agent_client"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["resource"] == [RESOURCE]
        assert query["redirect_uri"] == [result["callback_uri"]]
        assert "private-access" not in result["authorization_url"]
        with pytest.raises(ProviderError, match="state did not match"):
            await auth.complete_callback({"state": "wrong", "code": "code", "client_id": "bad"})
        assert fake.token_calls == 0 and auth.pending is not None
        fake.nonce = auth.pending.nonce
        await auth.complete_callback(
            {
                "state": auth.pending.state,
                "code": "one-time-code",
                "client_id": "oaiapp_test",
            }
        )
        assert fake.forms[0]["client_id"] == ["oaiapp_test"]
        assert "client_secret" not in fake.forms[0]
        assert fake.forms[0]["code_verifier"]
        assert fake.forms[0]["redirect_uri"] == [result["callback_uri"]]
        assert await auth.access_token() == "access-1"
        assert auth.pending is None and auth.server is None
        record = read_private_json(auth.path)
        assert record["subject"] == "test-subject"
        assert stat.S_IMODE(auth.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(auth.path.parent.stat().st_mode) == 0o700
        assert not list(auth.path.parent.glob("*.tmp"))
        restarted = ChatGPTAuth(auth.path.parent, http, callback_port=0)
        assert await restarted.access_token() == "access-1"
        reconnect = await restarted.connect()
        assert parse_qs(urlsplit(reconnect["authorization_url"]).query)["client_id"] == [
            "oaiapp_test"
        ]
        await restarted.close()


@pytest.mark.asyncio
async def test_oauth_nonce_and_scope_fail_closed(tmp_path: Path) -> None:
    fake = FakeOAuth()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        auth = ChatGPTAuth(tmp_path, http, callback_port=0)
        await auth.connect()
        assert auth.pending is not None
        with pytest.raises(ProviderError) as failure:
            await auth.complete_callback(
                {
                    "state": auth.pending.state,
                    "code": "code",
                    "client_id": "oaiapp_test",
                }
            )
        assert failure.value.code == "invalid_identity"
        assert not auth.status()[0]
        assert read_private_json(auth.path)["client_id"] == "oaiapp_test"
        await auth.connect()
        assert auth.pending is not None
        fake.nonce, fake.scopes = auth.pending.nonce, "openid profile email"
        with pytest.raises(ProviderError) as failure:
            await auth.complete_callback({"state": auth.pending.state, "code": "code"})
        assert failure.value.code == "plan_permission_missing"
        assert "credentials" not in read_private_json(auth.path)
        assert auth.pending is None


@pytest.mark.asyncio
async def test_oauth_returning_client_cannot_be_replaced(tmp_path: Path) -> None:
    fake = FakeOAuth()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        auth = ChatGPTAuth(tmp_path, http, callback_port=0)
        write_private_json(auth.path, connected_record())
        await auth.connect()
        assert auth.pending is not None
        with pytest.raises(ProviderError) as failure:
            await auth.complete_callback(
                {
                    "state": auth.pending.state,
                    "code": "code",
                    "client_id": "someone_else",
                }
            )
        assert failure.value.code == "client_mismatch"
        assert fake.token_calls == 0
        assert read_private_json(auth.path)["client_id"] == "oaiapp_test"
        assert await auth.access_token() == "private-access"


@pytest.mark.asyncio
async def test_actual_loopback_callback_completes_and_closes(tmp_path: Path) -> None:
    fake = FakeOAuth()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        auth = ChatGPTAuth(tmp_path, http, callback_port=0)
        result = await auth.connect()
        assert auth.pending is not None
        fake.nonce = auth.pending.nonce
        port = urlsplit(result["callback_uri"]).port
        assert port is not None
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        target = f"/auth/callback?state={auth.pending.state}&code=test&client_id=oaiapp_test"
        writer.write(f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        assert b"200 Result" in response and b"ChatGPT connected" in response
        assert b"access-1" not in response and b"refresh-1" not in response
        assert auth.server is None


@pytest.mark.asyncio
async def test_refresh_serialization_and_terminal_expiry(tmp_path: Path) -> None:
    fake = FakeOAuth()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        auth = ChatGPTAuth(tmp_path, http)
        record = connected_record()
        record["credentials"]["expires_at"] = 0
        write_private_json(auth.path, record)
        tokens = await asyncio.gather(*(auth.access_token() for _ in range(6)))
        assert tokens == ["access-1"] * 6
        assert fake.token_calls == 1
        assert read_private_json(auth.path)["credentials"]["refresh_token"] == "refresh-1"
        record = read_private_json(auth.path)
        record["credentials"]["expires_at"] = 0
        write_private_json(auth.path, record)
        fake.refresh_error = "refresh_token_reused"
        with pytest.raises(ProviderError) as failure:
            await auth.access_token()
        assert failure.value.code == "refresh_token_reused"
        saved = read_private_json(auth.path)
        assert "credentials" not in saved and saved["client_id"] == "oaiapp_test"


@pytest.mark.asyncio
async def test_rotated_token_survives_temporary_jwks_failure(tmp_path: Path) -> None:
    fake = FakeOAuth()
    fake.jwks_unavailable = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        auth = ChatGPTAuth(tmp_path, http)
        record = connected_record()
        record["credentials"]["expires_at"] = 0
        write_private_json(auth.path, record)
        with pytest.raises(ProviderError) as failure:
            await auth.access_token()
        assert failure.value.code == "identity_verification_unavailable"
        assert read_private_json(auth.path)["pending_refresh"]["refresh_token"] == "refresh-1"
        fake.jwks_unavailable = False
        restarted = ChatGPTAuth(tmp_path, http)
        assert await restarted.access_token() == "access-1"
        assert fake.token_calls == 1
        assert "pending_refresh" not in read_private_json(auth.path)


@pytest.mark.asyncio
async def test_expired_pending_identity_requires_reconnect_instead_of_staying_connected(
    tmp_path: Path,
) -> None:
    fake = FakeOAuth()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        auth = ChatGPTAuth(tmp_path, http)
        record = connected_record()
        record["credentials"]["expires_at"] = 0
        record["pending_refresh"] = {
            "access_token": "latest-access",
            "refresh_token": "latest-refresh",
            "expires_in": 3600,
            "_received_at": time.time() - 7200,
            "scope": fake.scopes,
            "id_token": jwt.encode(
                {
                    "iss": AUTH_ORIGIN,
                    "sub": "test-subject",
                    "aud": "oaiapp_test",
                    "exp": time.time() - 1,
                },
                fake.key,
                algorithm="RS256",
                headers={"kid": "test-key"},
            ),
        }
        write_private_json(auth.path, record)
        for _ in range(2):
            with pytest.raises(ProviderError) as failure:
                await auth.access_token()
            assert failure.value.code == "sign_in_required"
        assert not auth.status()[0]
        assert "Reconnect" in auth.status()[1]
        assert fake.token_calls == 0
        saved = read_private_json(auth.path)
        assert saved["client_id"] == "oaiapp_test"
        assert not {"credentials", "pending_refresh", "scopes", "id_token"} & saved.keys()


@pytest.mark.asyncio
async def test_disconnect_revokes_or_reports_unconfirmed_revocation(tmp_path: Path) -> None:
    fake = FakeOAuth()
    fake.revocation_fails = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        auth = ChatGPTAuth(tmp_path, http)
        write_private_json(auth.path, connected_record())
        await auth.disconnect()
        assert not auth.status()[0]
        assert "not confirmed" in auth.status()[1]
        saved = read_private_json(auth.path)
        assert saved["client_id"] == "oaiapp_test" and "credentials" not in saved
        assert "id_token" not in saved


def test_catalogue_context_does_not_send_nutrition(foods: list[dict[str, Any]]) -> None:
    context = compact_catalogue(foods)
    assert "nutrients" not in context[0] and "kcal" not in json.dumps(context)
    assert {"pack", "g", "kg"} <= set(context[0]["units"])


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"food_id": "invented"}, "unknown_food"),
        ({"candidates": ["invented"]}, "unknown_food"),
        ({"quantity": "0"}, "invalid_proposal"),
        ({"quantity": "-2"}, "invalid_proposal"),
        ({"quantity": "NaN"}, "invalid_proposal"),
        ({"quantity": 2}, "invalid_proposal"),
        ({"unit": "ml"}, "unsupported_unit"),
        ({"food_id": None}, "invalid_proposal"),
        ({"kcal": "100"}, "invalid_proposal"),
    ],
)
def test_proposals_cannot_change_facts_or_invent_ids(
    foods: list[dict[str, Any]], changes: dict[str, Any], code: str
) -> None:
    with pytest.raises(ProviderError) as failure:
        validate_interpretation(proposal(**changes), foods)
    assert failure.value.code == code


def test_missing_quantity_requires_clarification_or_explicit_saved_serving(
    foods: list[dict[str, Any]],
) -> None:
    value = validate_interpretation(proposal(quantity=None, unit=None), foods)
    assert value.outcome == "clarification" and value.items[0].quantity is None
    foods[0]["usual_serving"] = {"quantity": "1.5", "unit": "pack"}
    value = validate_interpretation(proposal(quantity=None, unit=None), foods)
    assert value.outcome == "proposal" and value.items[0].quantity == "1.5"


def test_partial_match_and_no_match_remain_explicit(foods: list[dict[str, Any]]) -> None:
    item = json.loads(proposal())["items"][0]
    missing = {
        "status": "no_match",
        "food_id": None,
        "quantity": None,
        "unit": None,
        "question": "Add this food to your catalogue?",
        "candidates": [],
    }
    value = validate_interpretation(
        json.dumps(
            {
                "outcome": "proposal",
                "message": "",
                "items": [item, missing],
            }
        ),
        foods,
    )
    assert value.outcome == "clarification" and value.items[1].status == "no_match"
    value = validate_interpretation(
        json.dumps(
            {
                "outcome": "proposal",
                "message": "",
                "items": [missing],
            }
        ),
        foods,
    )
    assert value.outcome == "no_match"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.test/image.png",
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:image/png;base64,not_valid!",
        "data:image/png;base64,aGVsbG8=",
    ],
)
def test_images_reject_remote_urls_and_invalid_types(value: str) -> None:
    with pytest.raises(ValidationError):
        AIInput(image_data_url=value)


def test_extract_preserves_unknowns_and_rejects_bad_values() -> None:
    result = validate_extraction(
        json.dumps(
            {
                "draft": {"name": "Example", "nutrients": {"kcal": "123", "protein": None}},
                "warnings": ["Basis is unreadable."],
            }
        )
    )
    assert result.draft.nutrients.protein is None and result.draft.basis_unit is None
    with pytest.raises(ProviderError):
        validate_extraction('{"draft":{"nutrients":{"kcal":"-1"}},"warnings":[]}')


def bounded_label() -> dict[str, Any]:
    return {
        "draft": {
            "name": "Synthetic baking potatoes",
            "preparation": "Oven baked, eaten with skin",
            "basis_amount": "100",
            "basis_unit": "g",
            "nutrients": {
                "kcal": "99",
                "fat": "<0.5g",
                "saturates": "<0.1 g",
                "carbohydrates": "20.5",
                "sugars": "1.4",
                "fibre": "2.6",
                "protein": "2.5",
                "salt": "<0.01",
            },
            "conversions": [],
            "usual_serving": None,
        },
        "warnings": ["The raw pack weight does not establish a cooked serving conversion."],
    }


def test_extract_retains_exact_nutrition_preparation_and_source_warnings() -> None:
    payload = bounded_label()
    result = validate_extraction(json.dumps(payload))
    assert result.draft.nutrients.model_dump() == {
        "kcal": "99",
        "fat": None,
        "saturates": None,
        "carbohydrates": "20.5",
        "sugars": "1.4",
        "fibre": "2.6",
        "protein": "2.5",
        "salt": None,
    }
    assert result.draft.preparation == "Oven baked, eaten with skin"
    assert result.draft.basis_amount == "100" and result.draft.basis_unit == "g"
    assert result.draft.conversions == [] and result.draft.usual_serving is None
    assert result.warnings[0] == payload["warnings"][0]
    assert len(result.warnings) == 2
    for evidence in ("Fat <0.5g", "Saturates <0.1 g", "Salt <0.01", "left unknown"):
        assert evidence in result.warnings[1]


@pytest.mark.parametrize(
    "reading", ["<0.5", " < 0.5 g ", "<=0.5", ">0.5", ">=0.5", "≤0.5", "≥ 0.5g"]
)
def test_extract_recognizes_only_nutrient_bounds(reading: str) -> None:
    result = validate_extraction(
        json.dumps({"draft": {"nutrients": {"fat": reading}}, "warnings": []})
    )
    assert result.draft.nutrients.fat is None
    assert reading.strip() in result.warnings[0]
    assert "Fat" in result.warnings[0]


def test_extract_preserves_exact_zero_and_full_warning_list() -> None:
    payload = bounded_label()
    payload["draft"]["nutrients"]["saturates"] = "0.00"
    payload["warnings"] = [f"Source warning {index}." for index in range(30)]
    result = validate_extraction(json.dumps(payload))
    assert result.draft.nutrients.saturates == "0.00"
    assert len(result.warnings) == 30
    assert result.warnings[:29] == payload["warnings"][:29]
    assert result.warnings[29].startswith(payload["warnings"][29])
    assert "Fat <0.5g" in result.warnings[29] and "Salt <0.01" in result.warnings[29]
    assert "Saturates" not in result.warnings[29]


@pytest.mark.parametrize(
    "reading",
    [
        "-1",
        "NaN",
        "0.5 g",
        "0,5",
        ".5",
        "1e-3",
        "trace",
        "0.1-0.5",
        "<secret>",
        "<0.5kcal",
        "<-1",
        "",
        0.5,
        False,
        {"value": "0.5"},
    ],
)
def test_extract_does_not_repair_other_invalid_nutrients(reading: Any) -> None:
    payload = bounded_label()
    payload["draft"]["nutrients"]["protein"] = reading
    with pytest.raises(ProviderError) as failure:
        validate_extraction(json.dumps(payload))
    assert failure.value.code == "invalid_food_draft"
    assert "Protein" in failure.value.message


@pytest.mark.parametrize(
    "field,value",
    [
        ("basis_amount", "<100"),
        ("basis_amount", "0"),
        ("nutrients", []),
        ("conversions", [{"unit": "pack", "amount": "1", "basis_amount": "<2000"}]),
        ("usual_serving", {"quantity": "0", "unit": "g"}),
        ("usual_serving", {"quantity": "<200", "unit": "g"}),
    ],
)
def test_bound_recovery_does_not_weaken_other_draft_fields(field: str, value: Any) -> None:
    payload = bounded_label()
    payload["draft"][field] = value
    with pytest.raises(ProviderError) as failure:
        validate_extraction(json.dumps(payload))
    assert failure.value.code == "invalid_food_draft"


@pytest.mark.parametrize("warnings", [None, "not a list", [1], ["warning"] * 31])
def test_bound_recovery_does_not_repair_invalid_warning_shape(warnings: Any) -> None:
    payload = bounded_label()
    payload["warnings"] = warnings
    with pytest.raises(ProviderError) as failure:
        validate_extraction(json.dumps(payload))
    assert failure.value.code == "invalid_food_draft"
    assert "Review notes" in failure.value.message


def test_extraction_errors_expose_only_static_field_labels() -> None:
    payload = bounded_label()
    payload["draft"]["nutrients"]["protein"] = "PRIVATE VALUE"
    payload["draft"]["nutrients"]["PRIVATE FIELD"] = "PRIVATE EXTRA"
    payload["draft"]["PRIVATE PROPERTY"] = "PRIVATE CONTENT"
    with pytest.raises(ProviderError) as failure:
        validate_extraction(json.dumps(payload))
    assert failure.value.code == "invalid_food_draft"
    assert "Protein" in failure.value.message and "Nutrients" in failure.value.message
    assert "PRIVATE" not in failure.value.message
    assert failure.value.__suppress_context__
    for text in ("PRIVATE MALFORMED JSON", "[]", '{"warnings":[]}', "[" * 1500):
        with pytest.raises(ProviderError) as failure:
            validate_extraction(text)
        assert failure.value.code == "invalid_food_draft"
        assert "PRIVATE" not in failure.value.message


def test_decimal_pattern_is_present_in_both_provider_schemas() -> None:
    original = Extraction.model_json_schema()
    chatgpt = strict_json_schema(original)
    llama = llama_json_schema(original)
    for schema, nutrients in (
        (chatgpt, chatgpt["$defs"]["DraftNutrients"]),
        (llama, llama["properties"]["draft"]["properties"]["nutrients"]),
    ):
        for field in ("kcal", "fat", "salt", "protein"):
            assert nutrients["properties"][field]["anyOf"] == [
                {"pattern": DECIMAL_PATTERN, "type": "string"},
                {"type": "null"},
            ]
        assert "{0,80}" not in json.dumps(schema)
    assert chatgpt["$defs"]["DraftConversion"]["properties"]["amount"]["pattern"] == DECIMAL_PATTERN
    assert chatgpt["$defs"]["DraftServing"]["properties"]["quantity"]["pattern"] == DECIMAL_PATTERN
    assert (
        chatgpt["$defs"]["FoodDraft"]["properties"]["basis_amount"]["anyOf"][0]["pattern"]
        == DECIMAL_PATTERN
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["chatgpt", "llama"])
async def test_extraction_service_recovers_bound_readings_and_preserves_contract(
    tmp_path: Path, provider: str
) -> None:
    observed: list[dict[str, Any]] = []
    reply = json.dumps(bounded_label())

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        observed.append(body)
        if provider == "chatgpt":
            return httpx.Response(
                200,
                text=sse(
                    {"type": "response.output_text.delta", "delta": reply},
                    {"type": "response.completed", "response": {"status": "completed"}},
                ),
            )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": reply}}]}
        )

    def unused_catalogue() -> list[dict[str, Any]]:
        raise AssertionError("Label extraction must not read a personal catalogue")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AIService(unused_catalogue, tmp_path, http)
    write_private_json(service.auth.path, connected_record())
    result = await service.extract(
        AIInput(text="Read the label", image_data_url=image_url(), provider=provider, model="test")
    )
    assert result["provider"] == provider
    assert result["draft"]["nutrients"]["kcal"] == "99"
    assert result["draft"]["nutrients"]["fat"] is None
    assert len(observed) == 1  # No automatic retry or fallback.
    instructions = (
        observed[0]["instructions"]
        if provider == "chatgpt"
        else observed[0]["messages"][0]["content"]
    )
    assert "raw pack weight does not establish a cooked serving conversion" in instructions
    assert "Never mix per-100g and per-serving figures" in instructions
    assert "Never substitute zero, the bare bound" in instructions
    await service.close()


def sse(*events: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


@pytest.mark.asyncio
async def test_chatgpt_image_request_requires_completed_stream(tmp_path: Path) -> None:
    observed: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(json.loads(request.content))
        return httpx.Response(
            200,
            text=sse(
                {"type": "response.output_text.delta", "delta": proposal()},
                {"type": "response.completed", "response": {"status": "completed"}},
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = ChatGPTAuth(tmp_path, http)
        write_private_json(auth.path, connected_record())
        providers = Providers(http, auth)
        text = await providers.generate(
            "chatgpt",
            "test-model",
            "http://unused/v1",
            AIInput(text="2 mango", image_data_url=image_url()),
            "instructions",
            Interpretation.model_json_schema(),
            "interpretation",
        )
    assert text == proposal()
    body = observed[0]
    assert body["store"] is False and body["stream"] is True
    assert body["input"][0]["content"][1] == {"type": "input_image", "image_url": image_url()}
    assert "temperature" not in body and "max_output_tokens" not in body
    assert body["text"]["format"]["strict"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal,code",
    [
        (None, "incomplete_response"),
        ({"type": "response.incomplete"}, "incomplete_response"),
        (
            {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "subscription_sharing_usage_limit_exceeded",
                        "message": "PRIVATE DETAILS",
                    }
                },
            },
            "subscription_sharing_usage_limit_exceeded",
        ),
    ],
)
async def test_partial_json_is_never_success(
    tmp_path: Path, terminal: dict[str, Any] | None, code: str
) -> None:
    events = [{"type": "response.output_text.delta", "delta": proposal()}]
    if terminal:
        events.append(terminal)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text=sse(*events))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = ChatGPTAuth(tmp_path, http)
        write_private_json(auth.path, connected_record())
        with pytest.raises(ProviderError) as failure:
            await Providers(http, auth).generate(
                "chatgpt",
                "test",
                "http://must-not-fallback/v1",
                AIInput(text="mango"),
                "instructions",
                Interpretation.model_json_schema(),
                "interpretation",
            )
    assert failure.value.code == code
    assert "PRIVATE DETAILS" not in failure.value.message
    assert requests == [f"{RESOURCE}/responses"]


@pytest.mark.asyncio
async def test_llama_image_wire_shape_and_service_validation(
    tmp_path: Path,
    foods: list[dict[str, Any]],
) -> None:
    observed: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": proposal()},
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AIService(lambda: foods, tmp_path, http)
    result = await service.interpret(
        AIInput(
            text="2 mango packs",
            image_data_url=image_url(),
            provider="llama",
            model="vision-test",
        )
    )
    assert result["items"][0]["quantity"] == "2"
    body = observed[0]
    assert body["messages"][1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": image_url()},
    }
    assert "nutrients" not in body["messages"][0]["content"]
    assert body["response_format"]["json_schema"]["strict"] is True
    assert not list(tmp_path.glob("*.png"))
    await service.close()


@pytest.mark.asyncio
async def test_settings_remember_model_per_provider_without_exposing_credentials(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "qwen-test",
                        "meta": {"multimodal": {"vision": True}},
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AIService(lambda: [], tmp_path, http)
    initial = await service.status()
    assert initial["provider"] == "chatgpt" and initial["model"] == ""
    await service.update_settings(AISettingsInput(provider="chatgpt", model="account-model"))
    local = await service.update_settings(AISettingsInput(provider="llama", model=""))
    assert local["model"] == "qwen-3.8-27b-vision-48k"
    cloud = await service.update_settings(AISettingsInput(provider="chatgpt", model=""))
    assert cloud["model"] == "account-model"
    assert "access_token" not in json.dumps(cloud) and "refresh_token" not in json.dumps(cloud)
    models = await service.providers.models("llama", "http://local/v1")
    assert models[0]["input_modalities"] == ["text", "image"]
    await service.close()


@pytest.mark.asyncio
async def test_llama_architecture_metadata_and_short_status_timeout(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions["timeout"]
        assert timeout["connect"] == 3 and timeout["read"] == 3
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "qwen-3.8-27b-vision-48k",
                        "architecture": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                        },
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AIService(lambda: [], tmp_path, http)
    assert (await service.status())["providers"][1]["connected"]
    models = await service.providers.models("llama", "http://local/v1")
    assert models[0]["input_modalities"] == ["text", "image"]
    await service.close()


@pytest.mark.asyncio
async def test_llama_truncated_completion_is_not_a_valid_proposal(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": proposal()},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProviderError) as failure:
            await Providers(http, ChatGPTAuth(tmp_path, http)).generate(
                "llama",
                "test",
                "http://local/v1",
                AIInput(text="mango"),
                "instructions",
                Interpretation.model_json_schema(),
                "interpretation",
            )
    assert failure.value.code == "incomplete_response"


@pytest.mark.asyncio
async def test_refresh_honors_future_time_and_does_not_discard_token(tmp_path: Path) -> None:
    fake = FakeOAuth()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        auth = ChatGPTAuth(tmp_path, http)
        record = connected_record()
        record["credentials"]["expires_at"] = time.time() - 10
        record["credentials"]["earliest_refresh_at"] = "2099-01-01T00:00:00Z"
        write_private_json(auth.path, record)
        with pytest.raises(ProviderError) as failure:
            await auth.access_token()
        assert failure.value.code == "refresh_not_ready"
        assert fake.token_calls == 0
        assert read_private_json(auth.path)["credentials"]["refresh_token"] == "private-refresh"


def test_oversized_photos_are_rejected_before_transport() -> None:
    value = (
        "data:image/png;base64,"
        + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES).decode()
    )
    with pytest.raises(ValidationError):
        AIInput(image_data_url=value)


def test_non_json_model_output_is_rejected(foods: list[dict[str, Any]]) -> None:
    for raw in ("Here is your meal", "```json\n{}\n```", "{}", "[]"):
        with pytest.raises(ProviderError) as failure:
            validate_interpretation(raw, foods)
        assert failure.value.code == "invalid_proposal"


def test_endpoint_rejects_credentials_and_queries() -> None:
    assert normalize_base_url("http://127.0.0.1:8080/v1/") == "http://127.0.0.1:8080/v1"
    for endpoint in (
        "file:///etc/passwd",
        "https://key:secret@host/v1",
        "https://host/v1?key=secret",
    ):
        with pytest.raises(ProviderError):
            normalize_base_url(endpoint)


def test_llama_wire_schema_avoids_large_repetitions_and_nested_refs(
    foods: list[dict[str, Any]],
) -> None:
    for contract in (Interpretation, Extraction):
        original = contract.model_json_schema()
        wire = llama_json_schema(original)
        encoded = json.dumps(wire)
        assert '"$ref"' not in encoded and '"$defs"' not in encoded
        assert '"minLength"' not in encoded and '"maxLength"' not in encoded
        assert wire["additionalProperties"] is False
        assert set(wire["required"]) == set(wire["properties"])
        assert original == contract.model_json_schema()
    assert (
        llama_json_schema(Interpretation.model_json_schema())["properties"]["items"]["maxItems"]
        == 30
    )
    with pytest.raises(ProviderError, match="invalid proposal"):
        validate_interpretation(proposal(question="x" * 1201), foods)


class EndlessStream(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes, delay: float = 0) -> None:
        self.chunk, self.delay = chunk, delay
        self.count = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(self.delay)
            self.count += 1
            yield self.chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["models", "llama", "chatgpt_error", "chatgpt_sse"])
async def test_streaming_reads_stop_before_buffering_unbounded_responses(
    tmp_path: Path, operation: str
) -> None:
    stream = EndlessStream(b"x" * READ_CHUNK_BYTES)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if operation == "chatgpt_error" else 200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = ChatGPTAuth(tmp_path, http)
        write_private_json(auth.path, connected_record())
        providers = Providers(http, auth)
        with pytest.raises(ProviderError) as failure:
            if operation == "models":
                await providers.models("llama", "http://local/v1")
            else:
                await providers.generate(
                    "llama" if operation == "llama" else "chatgpt",
                    "test-model",
                    "http://local/v1",
                    AIInput(text="test"),
                    "instructions",
                    Interpretation.model_json_schema(),
                    "interpretation",
                )
        assert failure.value.code == "provider_output_too_large"
    limit = MAX_PROVIDER_OUTPUT if operation == "chatgpt_sse" else MAX_JSON_BYTES
    assert stream.count <= limit // READ_CHUNK_BYTES + 1
    assert stream.closed


@pytest.mark.asyncio
async def test_sse_keepalives_cannot_extend_generation_deadline(tmp_path: Path) -> None:
    stream = EndlessStream(b": keepalive\n\n", delay=0.001)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as http:
        auth = ChatGPTAuth(tmp_path, http)
        write_private_json(auth.path, connected_record())
        providers = Providers(http, auth, generation_timeout=0.02)
        with pytest.raises(ProviderError) as failure:
            await providers.generate(
                "chatgpt", "test", "http://unused", AIInput(text="test"), "", {}, "test"
            )
        assert failure.value.code == "provider_timeout" and failure.value.status == 504
    assert stream.closed and stream.count > 0


@pytest.mark.asyncio
async def test_json_drip_feed_has_total_deadline() -> None:
    stream = EndlessStream(b" ", delay=0.001)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as http:
        with pytest.raises(ProviderError) as failure:
            await request_json(http, "GET", "http://local/models", timeout=0.02)
        assert failure.value.code == "provider_timeout"
    assert stream.closed and stream.count > 0


@pytest.mark.asyncio
async def test_unrequested_compression_is_rejected_before_body_decoding() -> None:
    stream = EndlessStream(b"not even a gzip stream")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProviderError) as failure:
            await request_json(http, "GET", "http://local/models")
        assert failure.value.code == "invalid_provider_response"
    assert stream.closed and stream.count == 0


def test_ranked_suggestions_require_confirmation_and_keep_scale_reading(
    foods: list[dict[str, Any]],
) -> None:
    result = validate_interpretation(
        proposal(quantity="180", unit="g", candidates=["demo-mango", "demo-mango"]), foods
    )
    item = result.items[0]
    assert result.outcome == item.status == "clarification"
    assert item.candidates == ["demo-mango"]
    assert item.food_id == "demo-mango"
    assert item.quantity == "180" and item.unit == "g"
    assert item.question


def test_visual_suggestions_cannot_escape_whitelist(foods: list[dict[str, Any]]) -> None:
    with pytest.raises(ProviderError) as error:
        validate_interpretation(proposal(candidates=["invented-potato"]), foods)
    assert error.value.code == "unknown_food"


def openrouter_catalogue() -> dict[str, Any]:
    def model(identifier: str, name: str, image: bool, structured: bool) -> dict[str, Any]:
        parameters = ["temperature", "structured_outputs"] if structured else ["temperature"]
        return {
            "id": identifier,
            "name": name,
            "architecture": {"input_modalities": ["text", "image"] if image else ["text"]},
            "supported_parameters": parameters,
        }

    return {
        "data": [
            model("zeta/vision", "Zeta Vision", True, True),
            model("text/only", "Text Only", False, True),
            model("no/schema", "No Schema", True, False),
            model("alpha/vision", "Alpha Vision", True, True),
            model("alpha/vision:batch", "Alpha Vision (batch)", True, True),
        ]
    }


def mock_http(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_openrouter_is_default_and_lists_only_usable_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KCALLM_OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("KCALLM_OPENROUTER_MODEL", "zeta/vision")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=openrouter_catalogue())

    service = AIService(lambda: [], tmp_path, mock_http(handler))
    settings = service.settings()
    assert settings["provider"] == "openrouter"
    assert settings["models"]["openrouter"] == "zeta/vision"

    models = await service.providers.models("openrouter", settings["llama_base_url"])
    # Photo-capable, structured-output models only, no routing variants, sorted by name.
    assert [item["id"] for item in models] == ["alpha/vision", "zeta/vision"]
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/models"
    assert seen[0].headers["Authorization"] == "Bearer or-test-key"
    await service.close()


@pytest.mark.asyncio
async def test_openrouter_key_never_goes_to_the_custom_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foods: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("KCALLM_OPENROUTER_API_KEY", "or-secret")
    monkeypatch.delenv("KCALLM_LLAMA_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": proposal()}}]}
        )

    service = AIService(lambda: foods, tmp_path, mock_http(handler))
    await service.update_settings(
        AISettingsInput(provider="llama", model="local", llama_base_url="https://example.test/v1")
    )
    seen.clear()  # Saving settings refreshes provider status; only the generations matter here.
    await service.interpret(AIInput(text="banana", provider="llama", model="local"))
    await service.interpret(AIInput(text="banana", provider="openrouter", model="alpha/vision"))

    custom, hosted = seen
    assert custom.url.host == "example.test"
    assert "Authorization" not in custom.headers
    assert str(hosted.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert hosted.headers["Authorization"] == "Bearer or-secret"
    assert json.loads(hosted.content)["model"] == "alpha/vision"
    await service.close()


@pytest.mark.asyncio
async def test_without_a_key_openrouter_is_listed_but_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KCALLM_OPENROUTER_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"No request expected, got {request.url}")

    service = AIService(lambda: [], tmp_path, mock_http(handler))
    assert service.settings()["provider"] == "chatgpt"  # Existing installs keep their default.
    with pytest.raises(ProviderError) as refused:
        service.resolve(AIInput(text="banana", provider="openrouter", model="any/model"))
    assert refused.value.code == "provider_not_configured"
    await service.close()
