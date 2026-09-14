from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent.api import app


client = TestClient(app)


def setup_function():
    client.cookies.clear()
    sign_up(client)


def test_interactive_ui_is_reachable_and_contains_the_app_shell():
    response = client.get("/ui")

    assert response.status_code == 200
    assert "OpportunityAI" in response.text
    # app shell: auth gate, profile switcher, notification bell, routed views
    assert "auth-view" in response.text
    assert "app-shell" in response.text
    assert "switcher-menu" in response.text
    assert "view-root" in response.text


def test_ui_exposes_the_agentic_workflow_routes():
    """The views the workflow actually needs - onboarding first, then a
    dashboard, a review queue, and a place to browse what was ruled out."""
    response = client.get("/ui")

    for route in ["#/onboarding", "#/dashboard", "#/review", "#/opportunities", "#/profile"]:
        assert route in response.text, f"{route} missing from the UI"


def test_ui_talks_to_the_per_profile_pipeline_endpoints():
    response = client.get("/ui")

    for endpoint in ["/profiles", "/summary", "/opportunities", "/notifications", "/run"]:
        assert endpoint in response.text, f"{endpoint} not referenced by the UI"






def test_onboarding_final_step_is_profile_type_aware():
    """A company finishing setup must not be told to "upload a CV first".

    Steps 1 and 2 both branch on the profile's subject, but step 3 was static
    HTML that always pointed at a CV - so a tender profile ended its own
    onboarding aimed at the wrong paperwork. It now reads /schema like the
    other steps, which is also what makes it correct for profile types that
    do not exist yet.
    """
    response = client.get("/ui")

    assert "async function renderOnboardStep3" in response.text, "step 3 must fetch the schema"
    assert "Upload company documents" in response.text, "company path missing"
    assert "Upload a CV first" in response.text, "person path missing"


def test_an_awarded_elsewhere_tender_shows_no_action_buttons():
    """A tender awarded to someone else must not still offer Approve, Submit
    or Apply anyway - there is nothing left for the owner to decide on it,
    whatever workflow stage it happened to be sitting at.
    """
    response = client.get("/ui")

    assert "if(o.awarded_to){" in response.text
    assert "Awarded to " in response.text


def test_ui_offers_password_recovery_without_ever_showing_a_password():
    """Someone locked out needs a way back in that is not "ask an admin".

    The account screen deliberately has no reveal-password control, and could
    not have one - auth.py stores bcrypt hashes.
    """
    response = client.get("/ui")

    assert "#/account" in response.text
    assert "Forgot your password?" in response.text
    assert "/forgot-password" in response.text
    assert "one-way hash" in response.text


def test_ui_can_delete_a_profile_and_says_what_that_destroys():
    response = client.get("/ui")

    assert "data-delprofile" in response.text
    # the confirmation names the consequences rather than just "are you sure?"
    assert "Delete the profile" in response.text
    assert "cannot be undone" in response.text


def test_ui_offers_assisted_input_that_asks_before_saving():
    """Typing a profile in field by field is the thing people abandon.

    The assist box takes free text; what comes back is offered for review
    rather than written straight in, so the model never silently overwrites
    what the owner typed about themselves.
    """
    response = client.get("/ui")

    assert "/assist" in response.text
    assert "Sort this into my profile" in response.text
    assert "asks you before saving" in response.text
    assert "data-sug" in response.text


def test_ui_carries_contextual_coach_tips_that_can_be_dismissed():
    response = client.get("/ui")

    assert "coachHtml" in response.text
    assert "dismissedTips" in response.text
    # tips are computed from profile state, not a fixed tour
    assert "praz-codes" in response.text
    assert "docs-missing-" in response.text


def test_ui_respects_reduced_motion():
    """The depth pass adds movement; anyone who has asked their OS not to
    animate things must not get it anyway."""
    response = client.get("/ui")

    assert "prefers-reduced-motion" in response.text


def test_a_failed_bootstrap_does_not_masquerade_as_being_logged_out():
    """The "refresh logs me out" bug.

    checkAuth used to wrap the identity check and the whole app bootstrap in
    one try/catch, so any transient failure showed the login form to a
    perfectly authenticated user. Only a 401 may do that now.
    """
    response = client.get("/ui")

    assert "showBootProblem" in response.text
    assert "You are still signed in" in response.text
    assert "err.status===401" in response.text


def test_mouse_movement_is_not_treated_as_activity():
    """A jittery trackpad must not hold a session open forever."""
    response = client.get("/ui")
    page = response.text

    # It may be *named* - the code says plainly why it is excluded - but it
    # must never be wired up.
    assert 'addEventListener("mousemove"' not in page
    assert '"mousemove"' not in page.split("function wireActivity()")[1][:800]
    # what does count: intent, and reading
    for evt in ["click", "keydown", "input", "submit", "hashchange", "scroll"]:
        assert evt in page


def test_the_user_is_warned_before_the_session_ends():
    response = client.get("/ui")

    assert "session-warning" in response.text
    assert "Keep me signed in" in response.text
    assert "sw-count" in response.text


def test_where_the_user_was_is_preserved_across_re_authentication():
    response = client.get("/ui")

    assert "captureState" in response.text
    assert "restoreState" in response.text
    assert "oaResume" in response.text
    # a password is never part of what gets stashed
    assert 'el.type==="password"' in response.text


def test_the_request_layer_dedupes_retries_and_reports():
    response = client.get("/ui")

    assert "inflight" in response.text          # deduplication
    assert "backoffDelay" in response.text      # retry with backoff
    assert "apiSWR" in response.text            # stale-while-revalidate
    assert "__oaNet" in response.text           # observability
    assert "document.hidden" in response.text   # smart polling pauses when unseen


def test_ui_lets_an_opportunity_be_binned_restored_and_deleted_for_good():
    """An agent working unattended produces volume - clearing the list has to
    be possible, recoverable, and separate from destroying the draft."""
    response = client.get("/ui")

    assert "#/bin" in response.text
    assert "data-restore" in response.text
    assert "data-purge" in response.text
    assert "/trash" in response.text
    # permanent deletion names what it takes with it
    assert "cannot be undone" in response.text


def test_ui_can_show_an_opportunitys_history():
    response = client.get("/ui")

    assert "/history" in response.text
    assert "historyLabel" in response.text
    assert "Moved to the bin" in response.text


# --------------------------------------------------------------------------
# SEO and branding
# --------------------------------------------------------------------------

def test_ui_carries_seo_tags_describing_the_service():
    """The only indexable page in the app - everything past login is a
    per-account view with nothing a search engine should rank."""
    response = client.get("/ui")

    assert '<meta name="description"' in response.text
    assert 'rel="canonical"' in response.text
    assert 'property="og:title"' in response.text
    assert 'property="og:description"' in response.text
    assert 'property="og:image"' in response.text
    assert 'name="twitter:card"' in response.text
    assert 'application/ld+json' in response.text
    assert '"@type": "SoftwareApplication"' in response.text


def test_ui_declares_a_pwa_manifest_and_icons():
    response = client.get("/ui")

    assert 'rel="manifest"' in response.text
    assert '/assets/manifest.json' in response.text
    assert 'rel="icon"' in response.text
    assert 'serviceWorker' in response.text
    assert '/assets/sw.js' in response.text


def test_ui_credits_meshcloud_consultants_on_every_page():
    """A plain sibling of both #auth-view and #app-shell, so it renders
    under whichever one is visible without either view needing to know it
    exists - logged in or not, it's on the page."""
    response = client.get("/ui")

    assert "Powered by Meshcloud Consultants" in response.text
    assert "/assets/meshcloud-logo.jpeg" in response.text
    # sits outside both view containers, not nested inside either
    auth_pos = response.text.index('id="auth-view"')
    shell_pos = response.text.index('id="app-shell"')
    footer_pos = response.text.index('id="brand-footer"')
    assert footer_pos > auth_pos
    assert footer_pos > shell_pos


def test_robots_txt_allows_the_public_page_and_blocks_the_api():
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert "Allow: /ui" in response.text
    assert "Disallow: /profiles" in response.text
    assert "Disallow: /notifications" in response.text
    assert "Sitemap:" in response.text
    assert "sitemap.xml" in response.text


def test_sitemap_lists_the_one_public_url():
    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "<loc>" in response.text
    assert "/ui</loc>" in response.text


def test_static_assets_are_actually_served():
    """The exact trap web/index.html itself hit once before: a file present
    in the source tree but missing from pyproject's package-data ships fine
    from an editable install and silently 404s from the built image."""
    for path in ("/assets/meshcloud-logo.jpeg", "/assets/manifest.json",
                "/assets/sw.js", "/assets/icon-512.png", "/assets/icon-192.png",
                "/assets/favicon.png"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} did not serve"


def test_the_manifest_itself_is_valid_and_points_at_real_icon_files():
    manifest = client.get("/assets/manifest.json").json()

    assert manifest["name"] == "OpportunityAI"
    assert manifest["start_url"] == "/ui"
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200
