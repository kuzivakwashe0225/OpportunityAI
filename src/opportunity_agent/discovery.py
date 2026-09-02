from urllib.parse import quote_plus

from .models import PersonalProfile


def build_search_queries(profile: PersonalProfile) -> list[str]:
    """Build bounded, profile-specific queries for permitted public discovery."""
    concepts = list(dict.fromkeys([
        profile.field,
        *profile.interests,
        *profile.goals,
    ]))
    concepts = [concept.strip() for concept in concepts if concept and concept.strip()]
    levels = [profile.study_level] if profile.study_level else ["graduate"]
    countries = profile.preferred_countries or ([profile.country] if profile.country else [])

    queries: list[str] = []
    for concept in concepts[:5]:
        for level in levels[:2]:
            country = countries[0] if countries else "international"
            queries.append(f"{concept} {level} scholarship {country}")
    return list(dict.fromkeys(queries))


def build_search_urls(profile: PersonalProfile) -> list[str]:
    return [f"https://www.google.com/search?q={quote_plus(query)}" for query in build_search_queries(profile)]
