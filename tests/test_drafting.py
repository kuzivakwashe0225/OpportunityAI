from datetime import date

from opportunity_agent.drafting import build_application_package
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
        "required_documents": ["transcript", "cv"],
        "evidence": ["official scholarship eligibility page"],
        "requirements_verified": True,
    }
    values.update(overrides)
    return Opportunity(**values)


def make_profile(**overrides):
    values = {
        "name": "Tendai Moyo",
        "country": "Zimbabwe",
        "age": 29,
        "study_level": "masters",
        "field": "Computer Science",
        "documents": ["transcript", "cv"],
        "interests": ["renewable energy"],
        "goals": ["build climate technology for Southern Africa"],
        "certificates": ["BSc Computer Science, University of Zimbabwe (2021)"],
        "work_history": [
            "Software Engineer, Acme Corp (2021-2024): built payments infrastructure"
        ],
    }
    values.update(overrides)
    return PersonalProfile(**values)


def test_checklist_reflects_matched_and_missing_requirements():
    opportunity = make_opportunity(required_documents=["transcript", "cv", "reference letter"])
    profile = make_profile()
    match = match_opportunity(opportunity, profile)

    package = build_application_package(profile, opportunity, match)

    met = {item.requirement for item in package.checklist if item.status == "met"}
    missing = {item.requirement for item in package.checklist if item.status == "missing"}
    assert any("country" in r.lower() for r in met)
    assert any("reference letter" in r.lower() for r in missing)


def test_checklist_marks_unknown_requirements_for_review():
    opportunity = make_opportunity(required_age_max=35)
    profile = make_profile(age=None)
    match = match_opportunity(opportunity, profile)

    package = build_application_package(profile, opportunity, match)

    unknown = {item.requirement for item in package.checklist if item.status == "unknown"}
    assert any("age" in r.lower() for r in unknown)


def test_cover_note_references_the_opportunity_and_profile_goals():
    opportunity = make_opportunity()
    profile = make_profile()
    match = match_opportunity(opportunity, profile)

    package = build_application_package(profile, opportunity, match)

    assert opportunity.title in package.cover_note
    assert profile.name in package.cover_note
    assert "climate technology" in package.cover_note.lower()


def test_cover_note_cites_a_certificate_when_relevant():
    opportunity = make_opportunity()
    profile = make_profile()
    match = match_opportunity(opportunity, profile)

    package = build_application_package(profile, opportunity, match)

    assert "BSc Computer Science" in package.cover_note


def test_cover_note_never_states_a_fact_the_profile_does_not_have():
    opportunity = make_opportunity()
    profile = make_profile(certificates=[], work_history=[], goals=[])
    match = match_opportunity(opportunity, profile)

    package = build_application_package(profile, opportunity, match)

    assert "Relevant qualifications" not in package.cover_note
    assert "Relevant experience" not in package.cover_note


def test_package_warns_when_opportunity_is_not_eligible():
    opportunity = make_opportunity(eligible_countries=["Kenya"])
    profile = make_profile()
    match = match_opportunity(opportunity, profile)

    package = build_application_package(profile, opportunity, match)

    assert package.warnings
    assert any("eligib" in warning.lower() for warning in package.warnings)


def test_package_warns_when_opportunity_needs_review():
    opportunity = make_opportunity(required_age_max=35)
    profile = make_profile(age=None)
    match = match_opportunity(opportunity, profile)

    package = build_application_package(profile, opportunity, match)

    assert package.warnings
    assert any("missing" in warning.lower() or "confirm" in warning.lower() for warning in package.warnings)


def test_eligible_package_has_no_warnings():
    opportunity = make_opportunity()
    profile = make_profile()
    match = match_opportunity(opportunity, profile)

    package = build_application_package(profile, opportunity, match)

    assert package.warnings == []


def test_package_carries_evidence_for_review():
    opportunity = make_opportunity(evidence=["official page: deadline 1 December 2026"])
    profile = make_profile()
    match = match_opportunity(opportunity, profile)

    package = build_application_package(profile, opportunity, match)

    assert package.evidence == opportunity.evidence


def test_package_is_keyed_to_the_opportunity_url():
    opportunity = make_opportunity()
    profile = make_profile()
    match = match_opportunity(opportunity, profile)

    package = build_application_package(profile, opportunity, match)

    assert str(package.opportunity_url) == str(opportunity.url)
