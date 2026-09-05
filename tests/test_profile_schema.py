"""What each opportunity type actually needs from the owner.

The point of this module is that picking "tenders" and picking "scholarships"
must produce genuinely different setup screens and different document
checklists - a tender bid needs a tax clearance certificate and has no use for
a study level, and vice versa.
"""

import pytest

from opportunity_agent import profile_schema as ps


def test_every_declared_profile_type_has_a_spec():
    from opportunity_agent.models_db import PROFILE_TYPES

    for profile_type in PROFILE_TYPES:
        assert ps.spec_for(profile_type) is not None


def test_tenders_are_an_organisation_not_a_person():
    assert ps.spec_for("tender").subject == "organisation"
    assert ps.spec_for("scholarship").subject == "individual"
    assert ps.spec_for("job").subject == "individual"


def test_grants_can_be_applied_for_as_either_a_person_or_a_company():
    """Grants genuinely go both ways - research grants to individuals, funding
    to organisations - so this is the one type where the owner has to say
    which, rather than us guessing and asking for the wrong documents."""
    assert ps.spec_for("grant").subject == "either"
    assert ps.resolve_subject("grant", {"subject": "organisation"}) == "organisation"
    assert ps.resolve_subject("grant", {"subject": "individual"}) == "individual"
    # unset falls back to individual rather than demanding company papers
    assert ps.resolve_subject("grant", {}) == "individual"


def test_a_fixed_subject_type_ignores_a_contradictory_stored_value():
    """A tender profile is an organisation even if stale JSON says otherwise."""
    assert ps.resolve_subject("tender", {"subject": "individual"}) == "organisation"


def test_tender_documents_are_company_compliance_papers():
    keys = {d.key for d in ps.documents_for("tender", {})}
    assert "certificate_of_incorporation" in keys
    assert "tax_clearance" in keys
    assert "praz_registration" in keys
    assert "company_profile" in keys
    assert "cv" not in keys


def test_scholarship_documents_are_personal_papers():
    keys = {d.key for d in ps.documents_for("scholarship", {})}
    assert "cv" in keys
    assert "transcript" in keys
    assert "tax_clearance" not in keys
    assert "certificate_of_incorporation" not in keys


def test_a_grant_profile_swaps_its_whole_document_checklist_on_subject():
    individual = {d.key for d in ps.documents_for("grant", {"subject": "individual"})}
    organisation = {d.key for d in ps.documents_for("grant", {"subject": "organisation"})}

    assert "cv" in individual and "cv" not in organisation
    assert "certificate_of_incorporation" in organisation
    assert "certificate_of_incorporation" not in individual


def test_field_sets_differ_between_a_person_and_a_company():
    person = {f.key for f in ps.fields_for("scholarship", {})}
    company = {f.key for f in ps.fields_for("tender", {})}

    assert "study_level" in person
    assert "study_level" not in company
    assert "praz_categories" in company
    assert "praz_categories" not in person
    # both still need somewhere to say who they are and where they are
    assert {"name", "country"} <= person
    assert {"name", "country"} <= company


def test_required_documents_are_a_subset_of_all_documents():
    for profile_type in ("scholarship", "job", "grant", "tender"):
        docs = ps.documents_for(profile_type, {})
        required = ps.required_document_keys(profile_type, {})
        assert required <= {d.key for d in docs}
        assert required, f"{profile_type} should require at least one document"


def test_missing_documents_reports_what_the_owner_still_owes_us():
    """The signal behind "please may I have these documents" - the agent has to
    be able to say precisely which ones, not just that something is absent."""
    held = {"company_profile", "tax_clearance"}
    missing = ps.missing_document_keys("tender", {}, held)

    assert "certificate_of_incorporation" in missing
    assert "company_profile" not in missing
    assert "tax_clearance" not in missing


def test_nothing_is_missing_once_everything_required_is_held():
    required = ps.required_document_keys("scholarship", {})
    assert ps.missing_document_keys("scholarship", {}, required) == set()


def test_document_labels_are_human_readable_for_the_upload_prompt():
    label = ps.document_label("tender", {}, "tax_clearance")
    assert "Tax Clearance" in label
    # and an unknown key degrades to the key itself rather than blowing up the
    # notification that is trying to ask for it
    assert ps.document_label("tender", {}, "mystery_paper") == "mystery_paper"


def test_search_vocabulary_is_type_specific():
    """The single biggest bug this module exists to kill: every profile type
    used to search for the literal word "scholarship"."""
    assert "scholarship" in ps.spec_for("scholarship").search_terms
    assert "tender" in ps.spec_for("tender").search_terms
    assert "scholarship" not in ps.spec_for("tender").search_terms
    assert "job" in " ".join(ps.spec_for("job").search_terms)


def test_unknown_profile_type_raises_rather_than_guessing():
    with pytest.raises(KeyError):
        ps.spec_for("nonsense")
