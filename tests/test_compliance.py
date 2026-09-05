"""Telling the owner exactly what still stands between them and a submission.

Every line this produces has to trace back to a fact somebody published or the
owner entered. "Arrange bid security of 25000" is useful because the tender
page says 25000. Inventing a plausible-sounding requirement would be worse
than saying nothing, because the owner would go and act on it.
"""

from datetime import date

from opportunity_agent import compliance, egp
from opportunity_agent.models import Opportunity


def make_tender_opportunity(**overrides) -> Opportunity:
    payload = {
        "source": "PRAZ eGP",
        "title": "Supply of transformers",
        "url": "https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/46190",
        "deadline": date(2026, 9, 17),
        "eligible_countries": ["Zimbabwe"],
        "required_categories": ["GE001"],
        "required_documents": list(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        "evidence": ["PRAZ eGP bulletin board listing"],
        "requirements_verified": True,
    }
    payload.update(overrides)
    return Opportunity(**payload)


def test_a_company_with_nothing_uploaded_is_told_every_paper_it_needs():
    report = compliance.build_report(
        profile_type="tender", fields={}, held_documents=set(),
        opportunity=make_tender_opportunity(),
    )

    missing = {item.key for item in report.items if item.status == "missing"}
    assert "tax_clearance" in missing
    assert "certificate_of_incorporation" in missing
    assert report.ready_to_submit is False


def test_papers_already_in_the_vault_are_not_asked_for_again():
    report = compliance.build_report(
        profile_type="tender", fields={},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(),
    )

    assert {item.key for item in report.items if item.status == "missing"} == set()


def test_the_advice_names_documents_the_way_the_tender_does():
    """"tax_clearance" is a database key. The owner is going to a filing
    cabinet, so the instruction has to say Tax Clearance Certificate."""
    report = compliance.build_report(
        profile_type="tender", fields={}, held_documents=set(),
        opportunity=make_tender_opportunity(),
    )

    text = " ".join(report.actions)
    assert "Tax Clearance" in text
    assert "tax_clearance" not in text


def test_money_the_bidder_must_put_up_becomes_an_action():
    details = egp.TenderDetails(bid_security_domestic=25000, spoc_fee=400, bid_form_fee=0)
    report = compliance.build_report(
        profile_type="tender", fields={},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(), details=details,
    )

    text = " ".join(report.actions)
    assert "25000" in text or "25,000" in text
    assert "400" in text
    # a zero fee is not an action item
    assert "Bid Form Fee" not in text


def test_a_zimbabwean_bidder_is_only_told_the_domestic_figure():
    """Tenders publish a domestic and an international bid security. The
    bidder pays one. Quoting both makes the owner work out which applies when
    their own profile already answers it."""
    details = egp.TenderDetails(bid_security_domestic=25000, bid_security_international=90000)
    report = compliance.build_report(
        profile_type="tender", fields={"country": "Zimbabwe"},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(), details=details, as_of=date(2026, 1, 1),
    )

    text = " ".join(report.actions)
    assert "25,000" in text
    assert "90,000" not in text


def test_an_unstated_country_shows_both_rather_than_guessing():
    details = egp.TenderDetails(bid_security_domestic=25000, bid_security_international=90000)
    report = compliance.build_report(
        profile_type="tender", fields={},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(), details=details, as_of=date(2026, 1, 1),
    )

    text = " ".join(report.actions)
    assert "25,000" in text and "90,000" in text


def test_addendums_are_raised_because_they_supersede_the_requirements():
    details = egp.TenderDetails(addendum_count=7)
    report = compliance.build_report(
        profile_type="tender", fields={},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(), details=details,
    )

    assert any("7" in action and "addend" in action.lower() for action in report.actions)


def test_no_addendums_produces_no_addendum_noise():
    report = compliance.build_report(
        profile_type="tender", fields={},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(), details=egp.TenderDetails(),
    )

    assert not any("addend" in action.lower() for action in report.actions)


def test_a_category_the_company_is_not_registered_for_is_a_blocker():
    report = compliance.build_report(
        profile_type="tender", fields={"praz_categories": ["SV001"]},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(required_categories=["GE001"]),
    )

    assert report.ready_to_submit is False
    assert any("GE001" in blocker for blocker in report.blockers)


def test_the_right_category_clears_the_blocker():
    report = compliance.build_report(
        profile_type="tender", fields={"praz_categories": ["ge001"]},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(required_categories=["GE001"]),
    )

    assert report.blockers == []
    assert report.ready_to_submit is True


def test_a_closing_date_gone_by_is_a_blocker_not_a_suggestion():
    report = compliance.build_report(
        profile_type="tender", fields={"praz_categories": ["GE001"]},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(deadline=date(2026, 1, 1)),
        as_of=date(2026, 9, 5),
    )

    assert report.ready_to_submit is False
    assert any("closed" in b.lower() or "expired" in b.lower() for b in report.blockers)


def test_a_deadline_close_enough_to_matter_is_flagged():
    report = compliance.build_report(
        profile_type="tender", fields={"praz_categories": ["GE001"]},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(deadline=date(2026, 9, 10)),
        as_of=date(2026, 9, 5),
    )

    assert any("5 days" in action for action in report.actions)


def test_it_works_for_a_person_too_not_only_companies():
    scholarship = Opportunity(
        source="Example Trust", title="Masters Scholarship",
        url="https://example.org/s", required_documents=["cv", "transcript"],
        evidence=["eligibility page"], requirements_verified=True,
    )
    report = compliance.build_report(
        profile_type="scholarship", fields={}, held_documents={"cv"},
        opportunity=scholarship,
    )

    assert {item.key for item in report.items if item.status == "missing"} == {"transcript"}
    assert any("Academic transcript" in action for action in report.actions)


def test_nothing_is_invented_when_there_is_nothing_to_say():
    """A fully-prepared bid produces no busywork."""
    report = compliance.build_report(
        profile_type="tender", fields={"praz_categories": ["GE001"]},
        held_documents=set(egp.STANDARD_ZW_TENDER_DOCUMENTS),
        opportunity=make_tender_opportunity(), details=egp.TenderDetails(),
        as_of=date(2026, 1, 1),
    )

    assert report.ready_to_submit is True
    assert report.blockers == []
