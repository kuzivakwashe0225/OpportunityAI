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
