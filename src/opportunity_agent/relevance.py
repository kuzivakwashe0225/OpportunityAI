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

# The vocabulary of a call for applications, in two strengths.
#
# The split exists because the first version of this used one flat list and a
# single hit, and a French appliance retailer, an Amazon search page, a casino
# and a Minecraft support forum all sailed through it - every one of them says
# "program" or "position" or "award" somewhere in its chrome. Measured against
# the live database, the rule below catches 25 more of those, and checked line
# by line there was not one real call among them.
#
# STRONG words belong to a call and to almost nothing else. One is enough.
_STRONG_WORDS = (
    "apply now", "how to apply", "application", "applicant", "deadline",
    "closing date", "eligibility", "eligible", "call for", "scholarship",
    "bursary", "fellowship", "grant", "tender", "request for quotation",
    "request for proposal", "vacancy", "vacancies", "job opening",
    "internship", "stipend", "funding opportunity",
)

# WEAK words turn up on ordinary commercial pages too. One alone means
# nothing; two different ones start to mean something.
#
# Grouped rather than listed flat, because spelling variants of one word are
# not two pieces of evidence. A first attempt at this counted matching
# strings, and "programme" scored twice by also containing "program" - which
# let a retailer's "browse our programme of offers" through as though it had
# said two separate things.
_WEAK_WORD_GROUPS = (
    ("apply",),
    ("submit",),
    ("award",),
    ("programme", "program"),
    ("position",),
    ("candidates",),
    ("qualification",),
    ("recruit", "hiring"),
    ("funding",),
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

    Still generous, but no longer naive. One unambiguous word - "deadline",
    "scholarship", "how to apply" - is enough on its own. Failing that, two
    different weaker ones are needed, because "program" or "award" alone is
    something every commercial page in the world says somewhere in its
    chrome. Anything too short to assess gets the benefit of the doubt.
    """
    body = f"{title}\n{text}".lower()
    if len(text.strip()) < _MIN_TEXT_TO_JUDGE:
        return True
    if any(word in body for word in _STRONG_WORDS):
        return True
    distinct = sum(
        1 for group in _WEAK_WORD_GROUPS if any(word in body for word in group)
    )
    return distinct >= 2


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
