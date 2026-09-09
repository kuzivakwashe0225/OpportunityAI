"""Turning free-typed notes into suggested profile field values.

The point of this path is that it follows the *profile's own schema*: a
tender profile gets asked for PRAZ categories and a registration number, a
scholarship profile for a study level. A generic CV parser would ask every
profile the same questions, which is the failure profile_schema.py exists to
prevent.
"""

import json

import httpx
from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import api, extraction_llm, profile_schema
from opportunity_agent.api import app

client = TestClient(app)


def setup_function():
    client.cookies.clear()


def _client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _reply(obj):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": json.dumps(obj)}}
        )
    return handler


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------

def test_the_prompt_asks_for_the_profiles_own_fields_not_a_fixed_list():
    company = extraction_llm.build_assist_prompt(profile_schema.fields_for("tender", {}), "notes")
    person = extraction_llm.build_assist_prompt(
        profile_schema.fields_for("scholarship", {}), "notes"
    )

    assert "praz_categories" in company
    assert "registration_number" in company
    assert "study_level" not in company

    assert "study_level" in person
    assert "praz_categories" not in person


def test_a_select_field_is_offered_only_its_own_options():
    prompt = extraction_llm.build_assist_prompt(
        profile_schema.fields_for("scholarship", {}), "notes"
    )

    assert "masters" in prompt
    assert "doctoral" in prompt


def test_the_prompt_tells_the_model_not_to_invent():
    prompt = extraction_llm.build_assist_prompt(profile_schema.fields_for("job", {}), "notes")

    assert "Never invent" in prompt


# --------------------------------------------------------------------------
# Coercion - the model's answer is never trusted as-is
# --------------------------------------------------------------------------

def test_values_are_coerced_into_the_shape_each_field_accepts():
    specs = profile_schema.fields_for("tender", {})
    handler = _reply({
        "name": "  Meshcloud (Pvt) Ltd  ",
        "employee_count": "12",
        "praz_categories": ["GE001", "SV002"],
        "years_trading": "not a number",
        "sectors": "ICT hardware",
    })

    out = extraction_llm.extract_profile_fields(
        "notes", field_specs=specs, client=_client_with(handler)
    )

    assert out["name"] == "Meshcloud (Pvt) Ltd"
    assert out["employee_count"] == 12
    assert out["praz_categories"] == ["GE001", "SV002"]
    # a number field that came back as prose is dropped, not passed through
    assert "years_trading" not in out
    # a list field given one string still becomes a list
    assert out["sectors"] == ["ICT hardware"]


def test_a_select_value_outside_its_options_is_dropped():
    specs = profile_schema.fields_for("scholarship", {})

    out = extraction_llm.extract_profile_fields(
        "notes", field_specs=specs, client=_client_with(_reply({"study_level": "postdoc"}))
    )

    assert "study_level" not in out


def test_a_select_value_is_accepted_case_insensitively():
    specs = profile_schema.fields_for("scholarship", {})

    out = extraction_llm.extract_profile_fields(
        "notes", field_specs=specs, client=_client_with(_reply({"study_level": "Masters"}))
    )

    assert out["study_level"] == "masters"


def test_nulls_and_empties_are_simply_absent():
    specs = profile_schema.fields_for("tender", {})

    out = extraction_llm.extract_profile_fields(
        "notes",
        field_specs=specs,
        client=_client_with(_reply({"name": None, "sectors": [], "country": "  "})),
    )

    assert out == {}


def test_unparseable_model_output_yields_no_suggestions():
    specs = profile_schema.fields_for("tender", {})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "not json"}})

    result = extraction_llm.extract_profile_fields(
        "notes", field_specs=specs, client=_client_with(handler)
    )

    assert result == {}


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------

def _tender_profile():
    return client.post(
        "/profiles", json={"profile_type": "tender", "display_name": "Tenders"}
    ).json()


def test_assist_returns_suggestions_without_saving_them(outbox, monkeypatch):
    """The form is the owner's own account of themselves. A local model's
    reading of their notes is a suggestion, never an overwrite."""
    sign_up(client, outbox=outbox)
    profile = _tender_profile()
    monkeypatch.setattr(
        api, "_assist_profile_fields",
        lambda text, specs: {"name": "Meshcloud", "praz_categories": ["GE001"]},
    )

    body = client.post(
        f"/profiles/{profile['id']}/assist", json={"text": "we are meshcloud, praz GE001"}
    ).json()

    assert body["suggested"] == {"name": "Meshcloud", "praz_categories": ["GE001"]}
    # nothing was written
    saved = client.get(f"/profiles/{profile['id']}").json()["fields"]
    assert saved.get("name") in (None, "")


def test_assist_flags_suggestions_that_would_overwrite_something(outbox, monkeypatch):
    sign_up(client, outbox=outbox)
    profile = _tender_profile()
    client.put(f"/profiles/{profile['id']}", json={"fields": {"name": "Already Typed Ltd"}})
    monkeypatch.setattr(
        api, "_assist_profile_fields",
        lambda text, specs: {"name": "Guessed Ltd", "country": "Zimbabwe"},
    )

    body = client.post(f"/profiles/{profile['id']}/assist", json={"text": "notes"}).json()

    assert body["conflicts"] == ["name"]


def test_assist_is_given_the_profiles_own_field_specs(outbox, monkeypatch):
    sign_up(client, outbox=outbox)
    profile = _tender_profile()
    seen = {}

    def capture(text, specs):
        seen["keys"] = {s.key for s in specs}
        return {}

    monkeypatch.setattr(api, "_assist_profile_fields", capture)
    client.post(f"/profiles/{profile['id']}/assist", json={"text": "notes"})

    assert "praz_categories" in seen["keys"]
    assert "study_level" not in seen["keys"]


def test_assist_rejects_empty_text(outbox):
    sign_up(client, outbox=outbox)
    profile = _tender_profile()

    response = client.post(f"/profiles/{profile['id']}/assist", json={"text": "   "})

    assert response.status_code == 422


def test_assist_reports_an_unreachable_model_as_503(outbox, monkeypatch):
    sign_up(client, outbox=outbox)
    profile = _tender_profile()

    def blow_up(text, specs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(api, "_assist_profile_fields", blow_up)

    response = client.post(f"/profiles/{profile['id']}/assist", json={"text": "notes"})

    assert response.status_code == 503


def test_another_account_cannot_use_your_profile_for_assist(outbox):
    sign_up(client, outbox=outbox)
    profile = _tender_profile()
    client.cookies.clear()
    sign_up(client, email="other@example.com", outbox=outbox)

    response = client.post(f"/profiles/{profile['id']}/assist", json={"text": "notes"})

    assert response.status_code == 404
