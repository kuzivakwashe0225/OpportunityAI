"""Deciding what is not an opportunity, before it becomes one.

Measured on the live database before this existed: of 468 stored
opportunities, 23 were `consent.youtube.com` cookie interstitials, 67 were
Microsoft support pages, 11 were Wikipedia articles, 10 were a French
appliance retailer and 6 were Yandex login screens. One YouTube video had a
cover letter drafted for it.

Nothing there is unsafe. It is a credibility problem, and a fatal one for a
product a stranger tries once: someone who signs up, waits for a search and is
shown a YouTube consent page as their first opportunity does not come back.

**Why this is deliberately narrow.** Trimming the search engine list in this
project once caused a total discovery outage - the remaining engines were both
rate-limited and the system found nothing at all, silently. The lesson was not
"never filter", it was "filter where the decision is visible and reversible".
So this module:

  * names hosts explicitly rather than inferring quality from a score,
  * rejects on evidence present in front of it, never on absence alone,
  * keeps anything it cannot read, because a page behind a 403 might be the
    tender of the year and a wrongly-dropped opportunity is worse than a
    wrongly-kept one,
  * and reports what it dropped, in the run record the owner can see.

The asymmetry in the third point is the whole design. A junk row costs the
owner one glance. A real call that never appears costs them the call.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Hosts that never carry a call for applications. Every one of these was
# observed in the live database, not imagined - which is why the list is short
# and why it should only grow the same way.
_JUNK_HOSTS = {
    # Video and social. A post *about* an opportunity is not the opportunity,
    # and the page we would store is a login wall or a consent screen.
    "youtube.com", "www.youtube.com", "m.youtube.com", "consent.youtube.com",
    "youtu.be",
    "facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com",
    "twitter.com", "x.com", "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "pinterest.com", "www.pinterest.com",
    "reddit.com", "www.reddit.com",
    # Reference works. Real information, never an application.
    "wikipedia.org", "en.wikipedia.org", "fr.wikipedia.org",
    "wiktionary.org", "en.wiktionary.org",
    # Vendor documentation and support, which search engines love to return
    # for any query containing a product name.
    "support.google.com", "support.microsoft.com", "learn.microsoft.com",
    "docs.microsoft.com", "answers.microsoft.com",
    # Sign-in and account gateways. Whatever was behind them, this is not it.
    "accounts.google.com", "wappass.baidu.com", "passport.yandex.ru",
    "sso.passport.yandex.ru", "login.microsoftonline.com",
}

# Host *prefixes* that mean "you have been bounced to a gateway". Matched on
# the first label only, so `login.example.org` is caught while
# `logistics.example.org` is not.
_JUNK_SUBDOMAINS = ("consent", "login", "signin", "sso", "passport", "auth", "accounts")

# Path fragments that say the same thing. Anchored to a path segment so
# `/consent` matches and `/consentino-scholarship` does not.
_JUNK_PATH_SEGMENTS = {
    "login", "signin", "sign-in", "log-in", "consent", "cookies", "cookie-policy",
    "privacy", "privacy-policy", "terms", "terms-of-service", "cart", "checkout",
    "basket", "unsubscribe", "logout",
}

# The vocabulary of a call for applications, across the kinds this system
# looks for. One hit is enough - the test is "does this page talk about
# applying at all", not "is this a good match", which is the eligibility
# matcher's job and happens later with far more context.
_OPPORTUNITY_WORDS = (
    "apply", "application", "applicant", "deadline", "closing date", "close on",
    "eligib", "eligibility", "scholarship", "bursary", "fellowship", "grant",
    "funding", "tender", "bid", "procurement", "proposal", "call for",
    "vacancy", "vacancies", "job opening", "recruit", "hiring", "position",
    "submit", "submission", "award", "stipend", "internship", "programme",
    "program", "candidates", "qualification",
)

# Below this there is not enough text to judge, so nothing is judged. Pages
# behind a 403 land here, and they are kept: see the module docstring.
_MIN_TEXT_TO_JUDGE = 200


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def junk_url_reason(url: str) -> str | None:
    """Why this URL cannot be an opportunity, or None if it might be.

    Runs before the page is fetched, so a rejection here also saves the
    request - which matters when a cycle is working through fifty results and
    the upstream site is slow to refuse us.
    """
    host = _host(url)
    if not host:
        return None

    if host in _JUNK_HOSTS:
        return f"{host} does not publish calls for applications"

    # Match the registered domain too, so `careers.facebook.com` is caught
    # without listing every subdomain anyone might return.
    parts = host.split(".")
    for size in (2, 3):
        if len(parts) >= size and ".".join(parts[-size:]) in _JUNK_HOSTS:
            return f"{host} does not publish calls for applications"

    if parts and parts[0] in _JUNK_SUBDOMAINS:
        return f"{host} is a sign-in or consent gateway"

    try:
        segments = {s.lower() for s in urlsplit(url).path.split("/") if s}
    except ValueError:
        segments = set()
    overlap = segments & _JUNK_PATH_SEGMENTS
    if overlap:
        return f"the page is a {sorted(overlap)[0]} page, not a listing"

    return None


def reads_like_an_opportunity(text: str, title: str = "") -> bool:
    """Whether the fetched page talks about applying for anything at all.

    Deliberately generous. It asks for one word out of thirty-odd, from a
    vocabulary spanning scholarships, tenders, grants and jobs, and it gives
    the benefit of the doubt to anything too short to assess. A page has to be
    both readable and entirely silent about applications to fail this.
    """
    body = f"{title}\n{text}".lower()
    if len(text.strip()) < _MIN_TEXT_TO_JUDGE:
        return True
    return any(word in body for word in _OPPORTUNITY_WORDS)


def summarise_skipped(reasons: list[str]) -> str | None:
    """One line for the run record, instead of fifty.

    The owner needs to know the agent threw things away and roughly what -
    silent filtering is how a discovery outage goes unnoticed for a week - but
    not a wall of URLs.
    """
    if not reasons:
        return None
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
    detail = "; ".join(f"{reason} ({count})" for reason, count in top)
    more = "" if len(counts) <= 3 else f"; and {len(counts) - 3} other reasons"
    return f"skipped {len(reasons)} result{'s' if len(reasons) != 1 else ''} - {detail}{more}"


_WHITESPACE = re.compile(r"\s+")


def tidy(text: str) -> str:
    """Collapse whitespace, for comparing short strings."""
    return _WHITESPACE.sub(" ", text).strip()
