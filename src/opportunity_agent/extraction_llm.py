"""LLM-based extraction of profile facts from uploaded documents (CVs, etc).

Uses a local Ollama instance - SOLUTION_DEFINITION.md §14's deferred
"document extraction," now unblocked. Client is injectable, same
testability pattern as search.py/documents.py: tests never need a real
Ollama instance running.

Model choice is a real operational constraint, not a preference: the
deployment server has 11GB RAM shared with several other live projects and
no GPU. The installed 27B model OOM-killed the Ollama process on first live
test; qwen2.5:0.5b (397MB) is what's actually verified safe to run there
(confirmed live: memory barely moved across a real call). Extraction quality
is correspondingly modest for a model this small - conservative parsing
(returning less rather than guessing) matters more than usual here.
"""

from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, Field

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:0.5b"
_MAX_INPUT_CHARS = 6000

_PROMPT_TEMPLATE = """You extract facts from a CV or personal document. Read the text below and \
return ONLY a JSON object with these fields. Use [] or null for anything not clearly present - \
never invent a value that isn't in the text.

{{
  "work_history": [one string per role, e.g. "Software Engineer, Acme Corp (2021-2024): built payments infrastructure"],
  "certificates": [one string per qualification or certificate],
  "study_level": one of "undergraduate", "bachelors", "masters", "doctoral", or null,
  "field": string (field of study) or null
}}

Document text:
---
{text}
---

JSON:"""


class ExtractedFacts(BaseModel):
    work_history: list[str] = Field(default_factory=list)
    certificates: list[str] = Field(default_factory=list)
    study_level: str | None = None
    field: str | None = None


def extract_facts_from_text(
    text: str,
    *,
    base_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    client: httpx.Client | None = None,
) -> ExtractedFacts:
    prompt = _PROMPT_TEMPLATE.format(text=text[:_MAX_INPUT_CHARS])

    owns_client = client is None
    http_client = client or httpx.Client(timeout=120.0)  # local CPU inference can be slow
    try:
        response = http_client.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http_client.close()

    try:
        parsed = json.loads(payload.get("response", ""))
    except (json.JSONDecodeError, TypeError):
        return ExtractedFacts()

    if not isinstance(parsed, dict):
        return ExtractedFacts()

    return ExtractedFacts(
        work_history=parsed.get("work_history") or [],
        certificates=parsed.get("certificates") or [],
        study_level=parsed.get("study_level") or None,
        field=parsed.get("field") or None,
    )
