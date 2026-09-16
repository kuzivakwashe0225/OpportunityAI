"""Clicking an opportunity, and getting something worth clicking through to.

Written after the owner reported that searching and applying "is not coming
out the right way". Two faults sat behind that, and they compounded:

  1. Fetched pages were stored as raw HTML. One scholarship page was kept as
     257,822 characters of doctype, IE conditional comments, inline scripts
     and navigation chrome.
  2. Nothing read them anyway. extraction.py stores the page as
     `evidence=[content]` and `_page_text_for` only ever looked at `content`,
     `page_text`, `summary` and `description` - so on live data 399 of 400
     opportunities had no readable text, the requirement extractor found no
     sections in nothing, and every application came out as the same generic
     cover letter.

The third fault was that there was nowhere to see any of it: the list showed
cards and linked straight out to the source site. These tests pin the fix -
readable text in, readable text out, and one endpoint that answers "can I
apply for this, and what is missing?"
"""

from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import models_db
from opportunity_agent import db as db_module
from opportunity_agent.api import _page_text_for, app
from opportunity_agent.connector import html_to_text

client = TestClient(app)


def setup_function():
    client.cookies.clear()
    sign_up(client)


PAGE = """<!doctype html>
<html><head><title>Call</title>
<style>.nav{color:red}</style>
<script>var tracker={id:42};function boot(){console.log("hi")}</script>
</head>
<body><nav><a href="/">Home</a></nav>
<h1>Call for Research Proposals</h1>
<p>Proposals must not exceed 3.5 pages in Times New Roman 12.</p>
<p>Submit to research.development@potraz.zw by 3 October 2026.</p>
</body></html>"""


# --------------------------------------------------------------------------
# Turning a page into words
# --------------------------------------------------------------------------

def test_script_and_style_contents_are_not_page_text():
    """The reason a regex that strips tags is not good enough.

    Removing only the angle brackets leaves the JavaScript source and the
    stylesheet sitting in the middle of the "text" - and that is what gets
    sent to a model with a limited context window instead of the call.
    """
    text = html_to_text(PAGE)

    assert "console.log" not in text
    assert "tracker" not in text
    assert "color:red" not in text


def test_the_words_that_matter_survive():
    text = html_to_text(PAGE)

    assert "Call for Research Proposals" in text
    assert "3.5 pages in Times New Roman 12" in text
    assert "research.development@potraz.zw" in text
    assert "3 October 2026" in text


def test_headings_do_not_run_into_the_sentence_after_them():
    assert "Call for Research Proposals\n" in html_to_text(PAGE)


def test_text_that_is_not_html_is_returned_unchanged():
    plain = "Proposals must not exceed 3.5 pages."

    assert html_to_text(plain) == plain


# --------------------------------------------------------------------------
# Finding the text that was always there
# --------------------------------------------------------------------------

def _stored(**payload):
    return models_db.StoredOpportunity(
        profile_id="p", canonical_url="https://example.org/call",
        payload=payload, match_status="eligible", match_score=10, match_reasons={},
    )


def test_the_call_text_is_read_out_of_evidence():
    """The bug in one line: extraction.py writes the page to `evidence`."""
    opportunity = _stored(title="Call", evidence=["Submit by 3 October 2026."])

    assert "3 October 2026" in _page_text_for(opportunity)


def test_evidence_left_over_as_raw_html_is_cleaned_on_the_way_out():
    """The 400 rows already in the database were stored before the connector
    extracted text. Cleaning on read makes them usable without re-fetching
    every one of them from a site that may now 403 us."""
    opportunity = _stored(title="Call", evidence=[PAGE])

    text = _page_text_for(opportunity)

    assert "console.log" not in text
    assert "3 October 2026" in text


def test_a_real_summary_still_wins_over_evidence():
    opportunity = _stored(title="Call", summary="The blurb.", evidence=["The page."])

    assert _page_text_for(opportunity) == "The blurb."


def test_an_opportunity_with_nothing_stored_falls_back_to_its_title():
    assert _page_text_for(_stored(title="Call")) == "Call"


# --------------------------------------------------------------------------
# The detail endpoint
# --------------------------------------------------------------------------

def _profile(profile_type="tender", name="Meshcloud Consultants"):
    profile = client.post(
        "/profiles", json={"profile_type": profile_type, "display_name": "Tenders"}
    ).json()
    client.put(f"/profiles/{profile['id']}", json={"fields": {
        "name": name, "country": "Zimbabwe", "registration_number": "12345/2020",
    }})
    return profile


def _opportunity(profile_id, **extra):
    session = db_module.SessionLocal()
    payload = {"title": "Supply of ICT Equipment", "source": "PRAZ eGP",
               "deadline": "3 October 2026", "evidence": [PAGE]}
    payload.update(extra.pop("payload", {}))
    row = models_db.StoredOpportunity(
        profile_id=profile_id, canonical_url="https://egp.praz.org.zw/tender/1",
        payload=payload, match_status="eligible", match_score=10,
        match_reasons={"matched": ["category GE001"]}, **extra,
    )
    session.add(row)
    session.commit()
    row_id = row.id
    session.close()
    return row_id


def _upload(profile_id, doc_type, filename):
    session = db_module.SessionLocal()
    session.add(models_db.Document(
        profile_id=profile_id, object_key=f"k/{filename}", doc_type=doc_type,
        original_filename=filename, content_type="application/pdf", size_bytes=1234,
    ))
    session.commit()
    session.close()


def test_the_page_shows_what_the_call_says_not_its_markup():
    profile = _profile()
    opp_id = _opportunity(profile["id"])

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements"
    ).json()

    assert "3 October 2026" in body["call_text"]
    assert "console.log" not in body["call_text"]
    assert body["title"] == "Supply of ICT Equipment"


def test_documents_the_owner_already_holds_are_matched_to_what_is_demanded():
    """The request in the owner's words: "the system should be able to take
    the required documents that would be required by the opportunity taking
    from the uploaded documents by the user"."""
    profile = _profile()
    _upload(profile["id"], "tax_clearance", "ITF263-2026.pdf")
    opp_id = _opportunity(profile["id"], payload={
        "required_documents": ["tax_clearance", "audited_financials"],
    })

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements"
    ).json()
    by_key = {d["key"]: d for d in body["required_documents"]}

    assert by_key["tax_clearance"]["held"] is True
    assert by_key["tax_clearance"]["filename"] == "ITF263-2026.pdf"
    assert by_key["audited_financials"]["held"] is False
    assert by_key["audited_financials"]["filename"] is None
    assert body["documents_ready"] == 1


def test_the_standing_compliance_pack_is_listed_even_when_the_advert_omits_it():
    """A tender that forgets to restate "certificate of incorporation" in its
    advert still needs one at submission."""
    profile = _profile()
    opp_id = _opportunity(profile["id"], payload={"required_documents": ["tax_clearance"]})

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements"
    ).json()
    by_key = {d["key"]: d for d in body["required_documents"]}

    assert by_key["tax_clearance"]["demanded_by_call"] is True
    assert by_key["certificate_of_incorporation"]["demanded_by_call"] is False
    assert by_key["certificate_of_incorporation"]["held"] is False


def test_each_required_document_is_labelled_the_way_the_owner_would_name_it():
    profile = _profile()
    opp_id = _opportunity(profile["id"], payload={"required_documents": ["tax_clearance"]})

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements"
    ).json()
    labels = {d["key"]: d["label"] for d in body["required_documents"]}

    assert labels["tax_clearance"] == "Tax Clearance Certificate (ITF263)"


def test_form_values_come_from_the_profile_and_only_from_the_profile():
    profile = _profile()
    opp_id = _opportunity(profile["id"])

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements"
    ).json()
    prefill = {f["key"]: f["value"] for f in body["prefill"]}

    assert prefill["name"] == "Meshcloud Consultants"
    assert prefill["registration_number"] == "12345/2020"
    assert prefill["email"] == "owner@example.com"
    # Never filled in, so never offered - the same rule the drafting prompt
    # works to: nothing is invented on the owner's behalf.
    assert "vat_number" not in prefill


def test_a_person_is_not_asked_for_a_company_registration_number():
    """profile_schema decides the questions; this endpoint must not
    reintroduce a universal form behind it."""
    profile = _profile(profile_type="scholarship", name="Isaiah Chikeya")
    opp_id = _opportunity(profile["id"])

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements"
    ).json()
    keys = {f["key"] for f in body["prefill"]}

    assert "registration_number" not in keys
    assert "name" in keys


def test_the_submission_spec_is_reported_once_the_call_has_been_read():
    profile = _profile()
    opp_id = _opportunity(profile["id"])
    session = db_module.SessionLocal()
    row = session.get(models_db.StoredOpportunity, opp_id)
    row.package = {"status": "ready", "spec": {
        "document_kind": "bid", "submit_to": "procurement@example.org",
        "sections": [{"title": "Technical Proposal", "guidance": "what you will supply"}],
        "format_rules": {"file_format": "PDF", "max_pages": 3.5},
    }, "sections": [{"title": "Technical Proposal", "body": "We will supply..."}]}
    session.commit()
    session.close()

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements"
    ).json()

    assert body["submission"]["document_kind"] == "bid"
    assert body["submission"]["sections"][0]["title"] == "Technical Proposal"
    assert body["submission"]["format_rules"]["max_pages"] == 3.5
    assert body["draft"]["sections"][0]["body"] == "We will supply..."


def test_an_undrafted_opportunity_reports_empty_rather_than_failing():
    """The page has to render for the 400 opportunities nobody has drafted."""
    profile = _profile()
    opp_id = _opportunity(profile["id"])

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements"
    ).json()

    assert body["submission"]["sections"] == []
    assert body["draft"]["status"] is None


def test_another_accounts_opportunity_is_not_readable():
    profile = _profile()
    opp_id = _opportunity(profile["id"])

    client.cookies.clear()
    sign_up(client, email="someone.else@example.com")
    mine = _profile()

    assert client.get(
        f"/profiles/{mine['id']}/opportunities/{opp_id}/requirements"
    ).status_code == 404
    assert client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements"
    ).status_code == 404
