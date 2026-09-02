from datetime import date

from opportunity_agent.extraction import result_to_opportunity
from opportunity_agent.extraction import page_to_opportunity
from opportunity_agent.connector import PublicPage
from opportunity_agent.search import SearchResult


def test_search_result_becomes_evidence_backed_opportunity():
    result = SearchResult(
        title="Global STEM Scholarship",
        url="https://scholarships.example.org/global-stem",
        content="Applications close 1 December 2026.",
        score=0.91,
    )

    opportunity = result_to_opportunity(result, verified=True)

    assert opportunity.title == "Global STEM Scholarship"
    assert opportunity.source == "scholarships.example.org"
    assert opportunity.evidence == ["Applications close 1 December 2026."]
    assert opportunity.sources == ["https://scholarships.example.org/global-stem"]


def test_result_without_content_requires_review_evidence():
    result = SearchResult(title="Unverified Award", url="https://example.org/award")

    opportunity = result_to_opportunity(result)

    assert opportunity.evidence == []


def test_result_parser_extracts_common_scholarship_requirements():
    result = SearchResult(
        title="Southern Africa Climate Fellowship",
        url="https://scholarships.example.org/climate",
        content=(
            "Applications close 1 December 2026. Open to citizens of Zimbabwe and South Africa. "
            "Applicants must be enrolled in a master's programme in computer science and be at most 35. "
            "Required documents: CV, transcript and reference letter."
        ),
    )

    opportunity = result_to_opportunity(result, verified=True)

    assert opportunity.deadline == date(2026, 12, 1)
    assert opportunity.eligible_countries == ["Zimbabwe", "South Africa"]
    assert opportunity.required_levels == ["masters"]
    assert opportunity.required_fields == ["computer science"]
    assert opportunity.required_age_max == 35
    assert opportunity.required_documents == ["cv", "transcript", "reference letter"]


def test_ambiguous_deadline_is_left_unset():
    result = SearchResult(
        title="Award",
        url="https://example.org/award",
        content="Applications close soon. See the official page for details.",
    )

    assert result_to_opportunity(result).deadline is None


def test_unverified_search_snippet_cannot_create_hard_requirements():
    result = SearchResult(
        title="Award",
        url="https://example.org/award",
        content="Open to citizens of Zimbabwe. Applications close 1 December 2026.",
    )

    opportunity = result_to_opportunity(result)

    assert opportunity.eligible_countries == []
    assert opportunity.deadline is None


def test_verified_page_preserves_retrieval_metadata():
    page = PublicPage(
        url="https://example.org/award",
        content="Applications close 1 December 2026.",
        retrieved_at="2026-09-03T10:00:00+00:00",
        sha256="abc123",
    )

    opportunity = page_to_opportunity(page, title="Award")

    assert opportunity.deadline == date(2026, 12, 1)
    assert opportunity.retrieved_at == page.retrieved_at
    assert opportunity.content_sha256 == page.sha256
    assert opportunity.requirements_verified is True