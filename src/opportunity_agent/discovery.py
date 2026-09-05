from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from . import profile_schema
from .models import PersonalProfile
from .search import SearchResult, search


def build_search_queries(profile, profile_type: str = "scholarship") -> list[str]:
    """Build bounded, profile-specific queries for permitted public discovery.

    `profile_type` decides the vocabulary. Until it was added, this function
    appended the literal word "scholarship" to every query for every profile
    type - so the job profile searched for scholarships, and so did the grant
    profile. The word now comes from profile_schema, which is also where a new
    opportunity type declares its own.

    Handles both a person and an organisation. They draw their query concepts
    from different fields (a company has sectors and past contracts, not a
    field of study) and only a person has a study level to qualify a query
    with, so the level clause is dropped entirely for a company rather than
    asking the web for a "masters tender".
    """
    spec = profile_schema.spec_for(profile_type)
    is_organisation = getattr(profile, "categories", None) is not None

    if is_organisation:
        concepts = [
            *profile.sectors,
            *profile.interests,
            *profile.goals,
            *profile.certifications,
        ]
        levels: list[str | None] = [None]
    else:
        concepts = [
            profile.field,
            *profile.interests,
            *profile.goals,
            *profile.certificates,
            *profile.work_history,
        ]
        levels = [profile.study_level] if profile.study_level else [None]

    concepts = [c.strip() for c in dict.fromkeys(concepts) if c and c.strip()]
    countries = profile.preferred_countries or (
        [profile.country] if profile.country else []
    )
    country = countries[0] if countries else "international"

    queries: list[str] = []
    for concept in concepts[:5]:
        for term in spec.search_terms[:2]:
            for level in levels[:2]:
                parts = [concept, level, term, country]
                queries.append(" ".join(p for p in parts if p))
    return list(dict.fromkeys(queries))


def discover(
    profile: PersonalProfile,
    *,
    api_key: str,
    search_fn: Callable[..., list[SearchResult]] = search,
    max_results: int = 5,
    profile_type: str = "scholarship",
) -> list[SearchResult]:
    """Search public web indexes using profile intent, deduplicating result URLs."""
    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for query in build_search_queries(profile, profile_type):
        for result in search_fn(query, api_key=api_key, max_results=max_results):
            try:
                canonical = canonicalize_url(result.url)
            except ValueError:
                continue
            if canonical in seen_urls:
                continue
            seen_urls.add(canonical)
            results.append(result)
    return results


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("search result URL must be an HTTP(S) URL")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))
