from datetime import date

from opportunity_agent.ingestion import merge_opportunity
from opportunity_agent.models import Opportunity


def test_duplicate_opportunities_merge_sources_without_losing_evidence():
    first = Opportunity(
        source="Foundation A",
        title="Global Scholars Award",
        url="https://a.example.org/award",
        deadline=date(2026, 10, 1),
        evidence=["page 1: deadline 1 October 2026"],
    )
    second = Opportunity(
        source="Foundation B",
        title="Global Scholars Award 2026",
        url="https://b.example.org/award",
        deadline=date(2026, 10, 1),
        evidence=["announcement: applications close 1 October 2026"],
        required_documents=["transcript"],
    )

    merged = merge_opportunity(first, second)

    assert merged.title == first.title
    assert merged.sources == ["Foundation A", "Foundation B"]
    assert len(merged.evidence) == 2
    assert merged.required_documents == ["transcript"]


def test_duplicate_merge_preserves_newer_verified_provenance():
    first = Opportunity(
        source="Foundation A",
        title="Award",
        url="https://a.example.org/award",
        evidence=["snippet"],
    )
    second = first.model_copy(update={
        "evidence": ["official page"],
        "requirements_verified": True,
        "retrieved_at": "2026-09-03T10:00:00+00:00",
        "content_sha256": "new-hash",
        "content_type": "text/html",
        "parser_version": "scholarship-regex-v1",
    })

    merged = merge_opportunity(first, second)

    assert merged.requirements_verified is True
    assert merged.content_sha256 == "new-hash"
    assert merged.parser_version == "scholarship-regex-v1"
