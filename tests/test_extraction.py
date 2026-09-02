from opportunity_agent.extraction import result_to_opportunity
from opportunity_agent.search import SearchResult


def test_search_result_becomes_evidence_backed_opportunity():
    result = SearchResult(
        title="Global STEM Scholarship",
        url="https://scholarships.example.org/global-stem",
        content="Applications close 1 December 2026.",
        score=0.91,
    )

    opportunity = result_to_opportunity(result)

    assert opportunity.title == "Global STEM Scholarship"
    assert opportunity.source == "scholarships.example.org"
    assert opportunity.evidence == ["Applications close 1 December 2026."]
    assert opportunity.sources == ["https://scholarships.example.org/global-stem"]


def test_result_without_content_requires_review_evidence():
    result = SearchResult(title="Unverified Award", url="https://example.org/award")

    opportunity = result_to_opportunity(result)

    assert opportunity.evidence == []