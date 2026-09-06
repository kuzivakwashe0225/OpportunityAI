"""Logging in to eGP as the account owner, with their permission.

Everything here runs against mocked HTTP. It is deliberately never pointed at
the real endpoint in tests: repeatedly posting invented credentials at a
government procurement system looks exactly like credential stuffing and could
lock the owner's real account.

The form shape below is taken from the real login page
(https://egp.praz.org.zw/Indexes/login): a CakePHP form posting to
/users/login with a `_csrfToken` hidden field and a session cookie.
"""

import httpx
import pytest

from opportunity_agent import egp_session

LOGIN_PAGE = """
<form method="post" accept-charset="utf-8" id="login_box" action="/users/login">
  <input type="hidden" name="_method" value="POST"/>
  <input type="hidden" name="_csrfToken" autocomplete="off" value="TOKEN-abc123"/>
  <input type="hidden" name="checkCount" class="form-control" id="checkcount"/>
  <input type="text" name="username" class="form-control" id="username"/>
  <input type="password" name="password" class="form-control" id="password"/>
  <input type="submit" class="btn" value="Log In">
</form>
"""

LOGGED_IN_PAGE = '<a href="/users/logout">Logout</a> Welcome, Meshcloud'
LOGIN_FAILED_PAGE = LOGIN_PAGE + "<div>Invalid username or password</div>"


def test_the_csrf_token_is_read_from_the_login_page():
    assert egp_session.parse_csrf_token(LOGIN_PAGE) == "TOKEN-abc123"


def test_a_login_page_without_a_token_is_reported_not_guessed():
    with pytest.raises(egp_session.LoginError, match="CSRF"):
        egp_session.parse_csrf_token("<form></form>")


def test_a_successful_login_returns_an_authenticated_session():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE)
        assert request.url.path == "/users/login"
        body = request.content.decode()
        assert "_csrfToken=TOKEN-abc123" in body
        assert "username=meshcloud" in body
        return httpx.Response(302, headers={"location": "/Dashboards/index"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    session = egp_session.log_in("meshcloud", "secret", client=client)

    assert session.authenticated is True


def test_wrong_credentials_raise_rather_than_returning_a_broken_session():
    """The dangerous version of this bug is a "session" that isn't logged in,
    which then downloads the public page and reports it as the bid pack."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE)
        return httpx.Response(200, text=LOGIN_FAILED_PAGE)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    with pytest.raises(egp_session.LoginError, match="rejected"):
        egp_session.log_in("meshcloud", "wrong", client=client)


def test_the_password_never_appears_in_the_error():
    """An exception string ends up in logs, in the API response, and in the
    UI. None of those are places for the owner's password."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE)
        return httpx.Response(200, text=LOGIN_FAILED_PAGE)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    with pytest.raises(egp_session.LoginError) as caught:
        egp_session.log_in("meshcloud", "sup3rs3cret", client=client)

    assert "sup3rs3cret" not in str(caught.value)


def test_login_identifies_itself_honestly():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.method] = request.headers.get("user-agent", "")
        if request.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE)
        return httpx.Response(302, headers={"location": "/Dashboards/index"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    egp_session.log_in("meshcloud", "secret", client=client)

    assert "OpportunityAI" in seen["POST"]
    assert "Mozilla" not in seen["POST"]


def test_a_server_error_during_login_is_a_login_error_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    with pytest.raises(egp_session.LoginError):
        egp_session.log_in("meshcloud", "secret", client=client)


# ---------------------------------------------------------------------------
# What the login is actually for: the bid pack behind it
# ---------------------------------------------------------------------------

DOC_PAGE = """
<table>
  <tr><td>Bid Data Sheet</td>
      <td><a href="/Tenders/downloadTenderDoc/46190/7788">Download</a></td></tr>
  <tr><td>Technical Specifications</td>
      <td><a href="/Tenders/downloadTenderDoc/46190/7789">Download</a></td></tr>
</table>
"""


def test_the_bid_pack_listing_is_parsed_into_named_documents():
    docs = egp_session.parse_tender_documents(DOC_PAGE)

    assert [d.name for d in docs] == ["Bid Data Sheet", "Technical Specifications"]
    assert docs[0].url == "https://egp.praz.org.zw/Tenders/downloadTenderDoc/46190/7788"


def test_an_empty_bid_pack_is_an_empty_list_not_an_error():
    assert egp_session.parse_tender_documents("<table></table>") == []


def test_fetching_the_bid_pack_requires_an_authenticated_session():
    """Guards the same failure as above from the other side: never hand back
    the public page as though it were the documents."""
    session = egp_session.EgpSession(client=httpx.Client(), authenticated=False)

    with pytest.raises(egp_session.LoginError, match="not logged in"):
        session.tender_documents("46190")


def test_fetching_the_bid_pack_returns_what_the_portal_lists():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "46190" in str(request.url)
        return httpx.Response(200, text=DOC_PAGE)

    session = egp_session.EgpSession(
        client=httpx.Client(transport=httpx.MockTransport(handler)), authenticated=True
    )
    docs = session.tender_documents("46190")

    assert len(docs) == 2


def test_being_bounced_back_to_the_login_page_is_detected():
    """Sessions expire. A silently-expired one returns the login page with a
    200, which would otherwise be parsed as "this tender has no documents"."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LOGIN_PAGE)

    session = egp_session.EgpSession(
        client=httpx.Client(transport=httpx.MockTransport(handler)), authenticated=True
    )

    with pytest.raises(egp_session.LoginError, match="expired"):
        session.tender_documents("46190")
