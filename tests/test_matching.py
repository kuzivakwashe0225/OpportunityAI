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
        "requirements_verified": True,
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


def test_a_missing_document_is_a_request_not_a_rejection():
    """Deliberate behaviour change (was: hard failure -> "ineligible").

    "You do not qualify" and "you qualify but I still need your reference
    letter" are different answers, and only the second one is fixable by the
    owner in two minutes. Collapsing them into "ineligible" threw away
    winnable opportunities silently. Documents now report on their own
    dimension so the agent can go and ask for the file instead.
    """
    result = match_opportunity(
        make_opportunity(required_documents=["transcript", "cv", "reference"]),
        make_profile(),
    )

    assert result.missing_documents == ["reference"]
    assert not any("reference" in item.lower() for item in result.failed_requirements)
    assert result.status != "ineligible"


def test_documents_already_held_are_not_asked_for_again():
    result = match_opportunity(
        make_opportunity(required_documents=["transcript", "cv"]),
        make_profile(),
    )

    assert result.missing_documents == []
    assert result.status == "eligible"


def make_tender(**overrides):
    """A tender-shaped opportunity: no study level, no field, no age cap.

    Reusing the scholarship fixture here was a mistake worth keeping a note
    about - it asked a company for its study level and produced needs_review,
    which is correct behaviour on a nonsensical input, not a matching bug.
    """
    payload = {
        "source": "PRAZ eGP",
        "title": "Supply of transformers",
        "url": "https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/1",
        "eligible_countries": ["Zimbabwe"],
        "required_documents": [],
        "evidence": ["PRAZ eGP bulletin board listing"],
        "requirements_verified": True,
    }
    payload.update(overrides)
    return Opportunity(**payload)


def test_a_company_without_the_required_supplier_category_is_ineligible():
    """The one hard, checkable rule in the system - PRAZ registration either
    covers the tender's category or it does not."""
    from opportunity_agent.models import OrganisationProfile

    company = OrganisationProfile(name="Meshcloud", country="Zimbabwe", categories=["GE001"])
    result = match_opportunity(make_tender(required_categories=["SV001"]), company)

    assert result.status == "ineligible"
    assert any("SV001" in item for item in result.failed_requirements)


def test_a_company_holding_any_one_of_the_accepted_categories_qualifies():
    from opportunity_agent.models import OrganisationProfile

    company = OrganisationProfile(name="Meshcloud", country="Zimbabwe", categories=["sp001"])
    result = match_opportunity(
        make_tender(required_categories=["SH001", "SP001", "SV001"]), company
    )

    assert result.status == "eligible"
    assert any("SP001" in item for item in result.matched_requirements)


def test_a_company_that_has_not_told_us_its_categories_needs_review():
    """Not ineligible - we simply don't know yet, and saying "you can't bid"
    on the strength of a blank field would hide real work."""
    from opportunity_agent.models import OrganisationProfile

    company = OrganisationProfile(name="Meshcloud", country="Zimbabwe")
    result = match_opportunity(make_tender(required_categories=["GE001"]), company)

    assert result.status == "needs_review"


def test_missing_evidence_requires_review():
    result = match_opportunity(make_opportunity(evidence=[]), make_profile())

    assert result.status == "needs_review"
    assert any("evidence" in item.lower() for item in result.unknown_requirements)


def test_populated_but_unverified_requirements_require_review():
    result = match_opportunity(
        make_opportunity(requirements_verified=False),
        make_profile(),
    )

    assert result.status == "needs_review"
    assert any("verified" in item.lower() for item in result.unknown_requirements)


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
