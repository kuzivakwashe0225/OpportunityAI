from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from .models import PersonalProfile
from .search import SearchResult, search


def build_search_queries(profile: PersonalProfile) -> list[str]:
    """Build bounded, profile-specific queries for permitted public discovery."""
    concepts = list(dict.fromkeys([
        profile.field,
        *profile.interests,
        *profile.goals,
            *profile.certificates,
            *profile.work_history,
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


def discover(
    profile: PersonalProfile,
    *,
    api_key: str,
    search_fn: Callable[..., list[SearchResult]] = search,
    max_results: int = 5,
) -> list[SearchResult]:
    """Search public web indexes using profile intent, deduplicating result URLs."""
    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for query in build_search_queries(profile):
        for result in search_fn(query, api_key=api_key, max_results=max_results):
            canonical = canonicalize_url(result.url)
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
