from datetime import date

from .models import MatchResult, Opportunity, PersonalProfile


def _contains(value: str | None, options: list[str]) -> bool | None:
    if value is None:
        return None
    normalized = value.casefold()
    return any(option.casefold() in normalized for option in options)


def _exact_match(value: str | None, options: list[str]) -> bool | None:
    """Whole-value match, case-insensitive.

    Country names must not use substring matching: "Niger" is a substring of
    "Nigeria", and the same trap catches Sudan/South Sudan and Guinea/Guinea-Bissau.
    """
    if value is None:
        return None
    normalized = value.casefold()
    return any(option.casefold() == normalized for option in options)


def match_opportunity(
    opportunity: Opportunity,
    profile: PersonalProfile,
    *,
    as_of: date | None = None,
) -> MatchResult:
    matched: list[str] = []
    failed: list[str] = []
    unknown: list[str] = []

    if not opportunity.evidence:
        unknown.append("source evidence is required")
    has_requirements = (
        opportunity.eligible_countries
        or opportunity.required_levels
        or opportunity.required_fields
        or opportunity.required_age_max is not None
        or opportunity.required_documents
    )
    if not opportunity.requirements_verified and not has_requirements:
        unknown.append("eligibility requirements have not been verified")
    elif opportunity.requirements_verified and not has_requirements:
        unknown.append("verified eligibility requirements were not found")

    reference_date = as_of or date.today()
    if opportunity.deadline and opportunity.deadline < reference_date:
        failed.append("opportunity is expired")

    if opportunity.eligible_countries:
        country_match = _exact_match(profile.country, opportunity.eligible_countries)
        if country_match is True:
            matched.append("country")
        elif country_match is False:
            failed.append(f"country must be one of: {', '.join(opportunity.eligible_countries)}")
        else:
            unknown.append("country is required")

    if opportunity.required_levels:
        level_match = _contains(profile.study_level, opportunity.required_levels)
        if level_match is True:
            matched.append("study level")
        elif level_match is False:
            failed.append(f"study level must be one of: {', '.join(opportunity.required_levels)}")
        else:
            unknown.append("study level is required")

    if opportunity.required_fields:
        field_match = _contains(profile.field, opportunity.required_fields)
        if field_match is True:
            matched.append("field of study")
        elif field_match is False:
            failed.append(f"field must include one of: {', '.join(opportunity.required_fields)}")
        else:
            unknown.append("field of study is required")

    if opportunity.required_age_max is not None:
        if profile.age is None:
            unknown.append("age is required")
        elif profile.age <= opportunity.required_age_max:
            matched.append("age")
        else:
            failed.append(f"age must be at most {opportunity.required_age_max}")

    profile_documents = {document.casefold() for document in profile.documents}
    for document in opportunity.required_documents:
        if document.casefold() in profile_documents:
            matched.append(f"document: {document}")
        else:
            failed.append(f"missing document: {document}")

    if failed:
        status = "ineligible"
        score = 0
    elif unknown:
        status = "needs_review"
        score = 0
    else:
        status = "eligible"
        interest_matches = sum(
            1
            for interest in opportunity.interests
            if any(interest.casefold() in profile_interest.casefold() for profile_interest in profile.interests)
        )
        interest_score = round(30 * interest_matches / len(opportunity.interests)) if opportunity.interests else 30
        score = 70 + interest_score

    return MatchResult(
        opportunity_url=opportunity.url,
        status=status,
        score=score,
        matched_requirements=matched,
        failed_requirements=failed,
        unknown_requirements=unknown,
    )
