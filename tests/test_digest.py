from datetime import date

from opportunity_agent.digest import build_digest
from opportunity_agent.matching import match_opportunity
from opportunity_agent.models import Opportunity, PersonalProfile


def make_opportunity(**overrides):
    values = {
        "source": "Example Foundation",
        "title": "STEM Leadership Scholarship",
        "url": "https://example.org/scholarship",
        "deadline": date(2026, 12, 1),
        "eligible_countries": ["Zimbabwe"],
        "required_documents": ["transcript"],
        "evidence": ["official scholarship eligibility page"],
        "requirements_verified": True,
    }
    values.update(overrides)
    return Opportunity(**values)


def make_profile(**overrides):
    values = {
        "name": "Test Applicant",
        "country": "Zimbabwe",
        "documents": ["transcript"],
    }
    values.update(overrides)
    return PersonalProfile(**values)


def test_digest_surfaces_eligible_opportunities_with_reasons():
    opportunity = make_opportunity()
    result = match_opportunity(opportunity, make_profile())

    digest = build_digest([(opportunity, result)])

    assert opportunity.title in digest
    assert str(opportunity.deadline) in digest
    assert "country" in digest.lower()


def test_digest_flags_needs_review_with_missing_fields():
    opportunity = make_opportunity(required_age_max=30)
    result = match_opportunity(opportunity, make_profile(age=None))

    digest = build_digest([(opportunity, result)])

    assert "review" in digest.lower()
    assert "age" in digest.lower()


def test_digest_omits_ineligible_opportunities():
    opportunity = make_opportunity(eligible_countries=["Kenya"])
    result = match_opportunity(opportunity, make_profile())

    digest = build_digest([(opportunity, result)])

    assert opportunity.title not in digest


def test_digest_orders_eligible_before_needs_review():
    eligible_opportunity = make_opportunity(title="Eligible Award")
    eligible_result = match_opportunity(eligible_opportunity, make_profile())

    review_opportunity = make_opportunity(title="Needs Review Award", required_age_max=30)
    review_result = match_opportunity(review_opportunity, make_profile(age=None))

    digest = build_digest(
        [(review_opportunity, review_result), (eligible_opportunity, eligible_result)]
    )

    assert digest.index("Eligible Award") < digest.index("Needs Review Award")


def test_empty_digest_says_nothing_new():
    digest = build_digest([])

    assert "no new" in digest.lower()
