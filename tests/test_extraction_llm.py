import json

import httpx
import pytest

from opportunity_agent.extraction_llm import ExtractedFacts, extract_facts_from_text


def _client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_extract_facts_sends_the_document_text_and_requests_json_format():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": "{}"})

    extract_facts_from_text("some CV text", client=_client_with(handler))

    assert captured["url"] == "http://localhost:11434/api/generate"
    assert captured["body"]["format"] == "json"
    assert captured["body"]["stream"] is False
    assert "some CV text" in captured["body"]["prompt"]


def test_extract_facts_parses_the_model_response_into_expected_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        model_output = json.dumps({
            "work_history": ["Software Engineer, Acme Corp (2021-2024)"],
            "certificates": ["BSc Computer Science, University of Zimbabwe (2021)"],
            "study_level": "masters",
            "field": "Computer Science",
        })
        return httpx.Response(200, json={"response": model_output})

    result = extract_facts_from_text("cv text", client=_client_with(handler))

    assert result == ExtractedFacts(
        work_history=["Software Engineer, Acme Corp (2021-2024)"],
        certificates=["BSc Computer Science, University of Zimbabwe (2021)"],
        study_level="masters",
        field="Computer Science",
    )


def test_extract_facts_never_invents_a_value_for_missing_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": json.dumps({"work_history": ["Only this"]})})

    result = extract_facts_from_text("cv text", client=_client_with(handler))

    assert result.work_history == ["Only this"]
    assert result.certificates == []
    assert result.study_level is None
    assert result.field is None


def test_extract_facts_normalizes_a_nested_model_list_into_one_role():
    """qwen occasionally wraps role fragments in a nested JSON list.

    A malformed response must not turn a valid uploaded document into a 502.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": json.dumps({
            "work_history": [[
                "Software Engineer",
                "Acme Corp (2021-2024): built payments infrastructure",
            ]],
        })})

    result = extract_facts_from_text("cv text", client=_client_with(handler))

    assert result.work_history == [
        "Software Engineer Acme Corp (2021-2024): built payments infrastructure"
    ]


def test_extract_facts_returns_empty_result_on_unparseable_model_output():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": "not valid json at all"})

    result = extract_facts_from_text("cv text", client=_client_with(handler))

    assert result == ExtractedFacts()


def test_extract_facts_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "model crashed"})

    with pytest.raises(httpx.HTTPStatusError):
        extract_facts_from_text("cv text", client=_client_with(handler))


def test_extract_facts_truncates_very_long_input():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": "{}"})

    extract_facts_from_text("x" * 50_000, client=_client_with(handler))

    assert len(captured["body"]["prompt"]) < 20_000
