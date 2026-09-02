from datetime import date

from opportunity_agent.matching import match_opportunity
from opportunity_agent.models import Opportunity, PersonalProfile


def make_opportunity(**overrides):
    values = {
        "source": "Example Foundation",
        "title": "STEM Leadership Scholarship",
        "url": "https://example.org/scholarship",
        "deadline": date(2026, 12, 1),
        "eligible_countries": ["Zimbabwe"],
        "required_levels": ["masters"],
        "required_fields": ["computer science"],
        "required_age_max": 35,
        "required_documents": ["transcript", "cv"],
        "evidence": ["official scholarship eligibility page"],
    }
    values.update(overrides)
    return Opportunity(**values)


def make_profile(**overrides):
    values = {
        "name": "Test Applicant",
        "country": "Zimbabwe",
        "age": 29,
        "study_level": "masters",
        "field": "Computer Science",
        "documents": ["transcript", "cv"],
        "interests": ["technology", "leadership"],
    }
    values.update(overrides)
    return PersonalProfile(**values)


def test_hard_eligibility_precedes_fit_score():
    result = match_opportunity(make_opportunity(), make_profile(country="Kenya"))

    assert result.status == "ineligible"
    assert result.score == 0
    assert any("country" in reason.lower() for reason in result.failed_requirements)


def test_missing_information_is_not_reported_as_eligible():
    result = match_opportunity(make_opportunity(), make_profile(age=None))

    assert result.status == "needs_review"
    assert result.score == 0
    assert any("age" in item.lower() for item in result.unknown_requirements)


def test_matching_profile_gets_explainable_score():
    result = match_opportunity(make_opportunity(), make_profile())

    assert result.status == "eligible"
    assert result.score > 0
    assert result.matched_requirements
    assert result.failed_requirements == []


def test_missing_document_is_a_hard_failure():
    result = match_opportunity(
        make_opportunity(required_documents=["transcript", "cv", "reference"]),
        make_profile(),
    )

    assert result.status == "ineligible"
    assert any("reference" in item.lower() for item in result.failed_requirements)


def test_missing_evidence_requires_review():
    result = match_opportunity(make_opportunity(evidence=[]), make_profile())

    assert result.status == "needs_review"
    assert any("evidence" in item.lower() for item in result.unknown_requirements)


def test_unverified_record_without_requirements_requires_review():
    result = match_opportunity(
        make_opportunity(
            eligible_countries=[],
            required_levels=[],
            required_fields=[],
            required_documents=[],
            required_age_max=None,
            evidence=["search result evidence"],
            requirements_verified=False,
        ),
        make_profile(),
    )

    assert result.status == "needs_review"
    assert any("eligibility" in item.lower() for item in result.unknown_requirements)


def test_expired_opportunity_is_ineligible():
    result = match_opportunity(
        make_opportunity(deadline=date(2026, 1, 1), evidence=["deadline evidence"]),
        make_profile(),
        as_of=date(2026, 9, 2),
    )

    assert result.status == "ineligible"
    assert any("expired" in item.lower() for item in result.failed_requirements)


def test_country_matching_does_not_substring_match():
    # "Niger" is a substring of "Nigeria" (and Sudan/South Sudan, Guinea/Guinea-Bissau
    # follow the same trap) — country eligibility must compare whole names, not text.
    result = match_opportunity(
        make_opportunity(eligible_countries=["Niger"]),
        make_profile(country="Nigeria"),
    )

    assert result.status == "ineligible"
    assert any("country" in reason.lower() for reason in result.failed_requirements)


def test_interest_fit_changes_score():
    broad = match_opportunity(
        make_opportunity(evidence=["eligibility evidence"], interests=["leadership"]),
        make_profile(interests=["technology"]),
    )
    strong = match_opportunity(
        make_opportunity(evidence=["eligibility evidence"], interests=["technology"]),
        make_profile(interests=["technology"]),
    )

    assert strong.score > broad.score
