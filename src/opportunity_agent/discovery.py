import time
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import profile_schema
from .models import PersonalProfile
from .search import SearchResult, SearxngDegraded, default_search_fn

# 429 (Too Many Requests) and 403 (Forbidden, how some engines phrase the same
# thing) both mean "the backend is telling us to stop asking", not "this one
# query failed". SearxngDegraded means something adjacent but distinct - the
# backend itself answered fine, but the engines behind it did not, so the
# result cannot be trusted as a real answer either. Both are treated the same
# way here and differently from any other per-query error: an ordinary error
# is skipped so one bad query does not cost the others (same discipline as
# egp.py's page-by-page tolerance); these stop the whole cycle outright and -
# just as importantly - are never handed to cache_set, because caching a
# degraded non-answer as if it were real is how "brave was rate-limited for
# five minutes" becomes "no grants exist" for the next six hours.
_RATE_LIMIT_STATUSES = {429, 403}


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


def _is_rate_limit_error(error: Exception) -> bool:
    if isinstance(error, SearxngDegraded):
        return True
    return isinstance(error, httpx.HTTPStatusError) and (
        error.response is not None and error.response.status_code in _RATE_LIMIT_STATUSES
    )


def discover(
    profile: PersonalProfile,
    *,
    api_key: str | None = None,
    search_fn: Callable[..., list[SearchResult]] | None = None,
    max_results: int = 5,
    profile_type: str = "scholarship",
    pause_seconds: float = 0.0,
    cache_get: Callable[[str], list[SearchResult] | None] | None = None,
    cache_set: Callable[[str, list[SearchResult]], None] | None = None,
) -> list[SearchResult]:
    """Search public web indexes using profile intent, deduplicating result URLs.

    search_fn defaults to None rather than a fixed function, and is resolved
    here via default_search_fn() on every call rather than once at import
    time. That is what lets SEARXNG_URL in the environment take effect on the
    next discovery cycle rather than the next deploy - a default bound at
    def-time would freeze in whichever backend was configured when this
    module first loaded. api_key is optional for the same reason: SearXNG
    needs none, and a caller resolved onto it should not have to invent one.

    Three things exist here specifically to be a considerate, rate-limit-
    aware caller of a shared or self-hosted search backend rather than a
    hammer, since a self-hosted SearXNG fans each query out to several
    upstream engines on this server's own behalf:

    cache_get/cache_set are optional hooks (pipeline.py supplies DB-backed
    ones) so a query already answered recently is never re-sent at all - the
    actual traffic pattern behind repeated risk is a profile's unchanged
    fields being re-searched every polling cycle, not new questions.

    pause_seconds waits between queries that do reach the network (never
    before a cache hit, which costs nothing), spreading a cycle's requests
    out instead of firing them in one burst - the same pattern egp.py already
    uses for board pagination.

    A 429 or 403 from any single query stops the rest of *this* cycle's
    queries outright rather than working through the list regardless -
    continuing to ask is how a temporary block becomes a longer one - and
    returns whatever was already found instead of discarding it. Any other
    per-query error is skipped, not fatal, so one bad query does not cost the
    others.
    """
    search_fn = search_fn or default_search_fn()
    results: list[SearchResult] = []
    seen_urls: set[str] = set()

    for index, query in enumerate(build_search_queries(profile, profile_type)):
        cached = cache_get(query) if cache_get else None
        if cached is not None:
            query_results = cached
        else:
            if index and pause_seconds:
                time.sleep(pause_seconds)
            try:
                query_results = search_fn(query, api_key=api_key, max_results=max_results)
            except Exception as error:
                if _is_rate_limit_error(error):
                    break
                continue
            if cache_set:
                cache_set(query, query_results)

        for result in query_results:
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
