"""What the agent refuses to call an opportunity.

Half these tests are about what it must NOT reject. That balance is
deliberate: trimming the search engine list in this project once caused a
total discovery outage, and the lesson was that a filter needs its false
positives pinned as hard as its true ones. A junk row costs the owner a
glance; a real call that never appears costs them the call.

Every host asserted as junk below was actually in the live database.
"""

import pytest

from opportunity_agent import relevance


# --------------------------------------------------------------------------
# What it rejects, and why each one was there
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,because", [
    ("https://consent.youtube.com/ml?continue=https://www.youtube.com/watch?v=x",
     "23 of these were stored, one with a cover letter drafted for it"),
    ("https://www.youtube.com/watch?v=abZ2BJlnPv0", "a video is not a call"),
    ("https://www.facebook.com/login?next=%2FPotraz.Zw", "a login wall"),
    ("https://en.wikipedia.org/wiki/Scholarship", "a reference article"),
    ("https://support.microsoft.com/en-us/office/apply-a-filter", "vendor documentation"),
    ("https://sso.passport.yandex.ru/push?uuid=1", "a sign-in gateway"),
    ("https://wappass.baidu.com/static/captcha/tuxing.html", "a captcha wall"),
    ("https://accounts.google.com/ServiceLogin", "a sign-in gateway"),
])
def test_pages_that_cannot_be_opportunities_are_refused(url, because):
    assert relevance.junk_url_reason(url) is not None, because


def test_a_subdomain_of_a_junk_host_is_refused_too():
    """Without this the list would need every subdomain anyone might return."""
    assert relevance.junk_url_reason("https://careers.facebook.com/jobs") is not None


def test_a_sign_in_gateway_on_any_host_is_refused():
    """Whatever was behind it, what we would store is the gateway."""
    assert relevance.junk_url_reason("https://login.example.org/portal") is not None
    assert relevance.junk_url_reason("https://consent.example.org/") is not None


def test_a_cookie_or_terms_page_is_refused():
    assert relevance.junk_url_reason("https://fund.example.org/privacy-policy") is not None
    assert relevance.junk_url_reason("https://shop.example.org/checkout") is not None


# --------------------------------------------------------------------------
# What it must never reject - the half that matters more
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://egp.praz.org.zw/egp-SW5kZXhlcy9nZXRUZW5kZXJz",
    "https://www.uz.ac.zw/index.php/scholarships",
    "https://potraz.gov.zw/call-for-research-proposals/",
    "https://www.chevening.org/scholarship/zimbabwe/",
    "https://mastercardfdn.org/all/scholars/",
    "https://www.linkedin.com/jobs/view/1234567",
    "https://ubalt.academicworks.com/opportunities/1234",
    "https://www.dfa.ie/irish-aid/fellowship-training-programme/",
])
def test_real_sources_are_left_alone(url):
    assert relevance.junk_url_reason(url) is None, url


def test_a_word_that_merely_starts_like_a_junk_segment_is_not_a_junk_segment():
    """`/consentino-scholarship` is a scholarship, not a consent screen."""
    assert relevance.junk_url_reason("https://fund.org/consentino-scholarship") is None


def test_a_host_that_merely_starts_like_a_gateway_is_not_one():
    assert relevance.junk_url_reason("https://logistics.example.org/tenders") is None


def test_an_unparseable_url_is_given_the_benefit_of_the_doubt():
    assert relevance.junk_url_reason("not a url at all") is None


# --------------------------------------------------------------------------
# The second gate: what the page actually says
# --------------------------------------------------------------------------

TENDER = (
    "Invitation to tender. Bidders must submit a valid tax clearance "
    "certificate and CR14. Closing date 3 October 2026." * 4
)
SHOP = (
    "Refrigerators, washing machines and televisions at the lowest prices. "
    "Free delivery on orders over 500 euros. Our stores are open seven days "
    "a week. Browse our catalogue for the latest offers on home appliances." * 4
)


def test_a_call_for_applications_passes():
    assert relevance.reads_like_an_opportunity(TENDER, "Invitation to tender")


@pytest.mark.parametrize("text,title", [
    ("Applications close on 3 October 2026. " * 20, "Masters funding"),
    ("Eligibility: citizens of Zimbabwe. " * 20, "Commonwealth award"),
    ("We are hiring a systems engineer. " * 20, "Vacancy"),
    ("Submit your concept note by email. " * 20, "Call for proposals"),
])
def test_every_kind_this_system_looks_for_passes(text, title):
    assert relevance.reads_like_an_opportunity(text, title)


def test_a_retailer_that_never_mentions_applying_is_refused():
    """A search engine will return a shop's front page for "grant". Only
    reading it settles that."""
    assert not relevance.reads_like_an_opportunity(SHOP, "Electro Depot")


def test_a_page_too_short_to_judge_is_kept():
    """Pages behind a 403 land here. One of them might be the tender of the
    year, and a wrongly-dropped opportunity is worse than a wrongly-kept
    one."""
    assert relevance.reads_like_an_opportunity("Access denied.", "Tender portal")


def test_the_title_alone_is_enough_to_save_a_page():
    """The body may be navigation chrome and a PDF link."""
    assert relevance.reads_like_an_opportunity(SHOP, "Call for proposals 2026")


# --------------------------------------------------------------------------
# Telling the owner
# --------------------------------------------------------------------------

def test_nothing_skipped_says_nothing():
    assert relevance.summarise_skipped([]) is None


def test_the_summary_is_one_line_not_fifty():
    summary = relevance.summarise_skipped(
        ["consent.youtube.com does not publish calls for applications"] * 23
        + ["the page never mentions applying for anything"] * 4
    )

    assert summary.startswith("skipped 27 results")
    assert "consent.youtube.com" in summary
    assert "(23)" in summary
    assert "\n" not in summary


# --------------------------------------------------------------------------
# The gates, in a real discovery cycle
# --------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from conftest import sign_up  # noqa: E402
from opportunity_agent import api  # noqa: E402
from opportunity_agent.connector import PublicPage  # noqa: E402
from opportunity_agent.search import SearchResult  # noqa: E402

client = TestClient(api.app)

REAL_CALL = (
    "Commonwealth Masters Scholarship. Open to citizens of Zimbabwe. Applicants "
    "must hold a first degree in computer science. Applications close 3 October "
    "2026. Required documents: CV, transcript."
)


def _profile():
    client.cookies.clear()
    sign_up(client, email="filter@example.com")
    profile = client.post("/profiles", json={
        "profile_type": "scholarship", "display_name": "S"}).json()
    client.put(f"/profiles/{profile['id']}", json={"fields": {
        "name": "Tendai Moyo", "country": "Zimbabwe", "study_level": "masters",
        "field": "Computer Science"}})
    return profile


def _run(monkeypatch, profile_id, results, pages):
    monkeypatch.setattr(api, "_pipeline_search", lambda p, api_key, **kw: results)

    def fetch(url, **kwargs):
        return PublicPage(url=url, content=pages[url],
                          retrieved_at="2026-01-01T00:00:00Z", sha256="x",
                          content_type="text/html")

    monkeypatch.setattr(api, "_pipeline_fetch", fetch)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    return client.post(f"/profiles/{profile_id}/run").json()


def test_a_consent_page_never_becomes_an_opportunity(monkeypatch):
    """The exact shape of what was in the live database: a real call and a
    YouTube consent screen, returned together by the same search."""
    profile = _profile()
    results = [
        SearchResult(title="Commonwealth Masters Scholarship",
                     url="https://example.org/commonwealth", snippet="", source="example.org"),
        SearchResult(title="YouTube", url="https://consent.youtube.com/ml?continue=x",
                     snippet="", source="youtube.com"),
    ]
    pages = {"https://example.org/commonwealth": REAL_CALL}

    run = _run(monkeypatch, profile["id"], results, pages)

    assert run["found"] == 2, "the search found two - that count is not filtered"
    assert run["added"] == 1, "only one of them was an opportunity"

    stored = client.get(f"/profiles/{profile['id']}/opportunities").json()
    assert len(stored) == 1
    assert "youtube" not in stored[0]["canonical_url"]


def test_the_junk_page_is_never_even_fetched(monkeypatch):
    """Gate one runs on the URL, so a slow upstream refusing us is never the
    cost of finding out."""
    profile = _profile()
    fetched = []

    def fetch(url, **kwargs):
        fetched.append(url)
        return PublicPage(url=url, content=REAL_CALL, retrieved_at="2026-01-01T00:00:00Z",
                          sha256="x", content_type="text/html")

    monkeypatch.setattr(api, "_pipeline_search", lambda p, api_key, **kw: [
        SearchResult(title="YouTube", url="https://consent.youtube.com/ml", snippet="",
                     source="youtube.com"),
        SearchResult(title="Scholarship", url="https://example.org/real", snippet="",
                     source="example.org"),
    ])
    monkeypatch.setattr(api, "_pipeline_fetch", fetch)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    client.post(f"/profiles/{profile['id']}/run")

    assert fetched == ["https://example.org/real"]


def test_a_page_that_never_mentions_applying_is_dropped_after_reading_it(monkeypatch):
    """Gate two. The URL looks perfectly ordinary; only the content settles
    it."""
    profile = _profile()
    results = [SearchResult(title="Electro Depot", url="https://www.electrodepot.fr/offres",
                            snippet="", source="electrodepot.fr")]

    run = _run(monkeypatch, profile["id"], results,
               {"https://www.electrodepot.fr/offres": SHOP})

    assert run["found"] == 1
    assert run["added"] == 0


def test_the_owner_is_told_what_was_thrown_away(monkeypatch):
    """Silent filtering is how a discovery outage goes unnoticed for a week."""
    profile = _profile()
    results = [
        SearchResult(title="YouTube", url=f"https://consent.youtube.com/ml?v={n}",
                     snippet="", source="youtube.com")
        for n in range(5)
    ]

    run = _run(monkeypatch, profile["id"], results, {})

    assert run["added"] == 0
    assert any("skipped 5 results" in f for f in run["failures"]), run["failures"]


def test_a_real_call_still_gets_through_all_of_it(monkeypatch):
    """The test that would have caught the outage: after every gate, an
    ordinary scholarship page is still stored, matched and drafted."""
    profile = _profile()
    results = [SearchResult(title="Commonwealth Masters Scholarship",
                            url="https://example.org/commonwealth", snippet="",
                            source="example.org")]

    run = _run(monkeypatch, profile["id"], results,
               {"https://example.org/commonwealth": REAL_CALL})

    assert run["added"] == 1
    stored = client.get(f"/profiles/{profile['id']}/opportunities").json()
    assert stored[0]["match_status"] == "eligible"
