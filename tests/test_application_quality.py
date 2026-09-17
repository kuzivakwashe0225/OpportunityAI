"""Whether what comes out reads like the applicant wrote it.

Written after the owner said the drafts were "not professional and good at
all", and described what they do instead: "I first list down the requirements
then I start drafting each requirement step by step according to my strengths,
qualifications and trainings... if there are forms to fill I fill them
effectively according to my profile, qualifications, achievements."

Reading that against the code found four things, none of which was the model:

  1. The applicant's CV was never shown to it. Documents were read once on
     upload, mined for a handful of profile fields, and the text thrown away.
     Drafting was handed six one-line fields - so of course the result read
     like a stranger wrote it. It was written by something that had never
     seen a word of their evidence.
  2. Every section was written in isolation, with no idea what the others
     said, so they repeated each other and contradicted each other.
  3. A form attached to the call was never downloaded. For many Zimbabwean
     tenders and bursaries that form *is* the application.
  4. The covering letter came from a template that cannot say why this
     applicant suits this call.

These tests pin the fixes. They assert on what reaches the model and what
comes back out of the plumbing, never on the model's prose - a test that
asserted particular sentences would be testing qwen2.5:3b, not this system.
"""

import httpx
import pytest

from opportunity_agent import application_form, evidence, section_drafting
from opportunity_agent.application_spec import FormatRules, SectionSpec, SubmissionSpec


class _Doc:
    def __init__(self, doc_type, text):
        self.doc_type = doc_type
        self.extracted_text = text


class _Profile:
    name = "Isaiah Kuzivakwashe Chikeya"
    country = "Zimbabwe"
    field = "computer systems engineering"
    study_level = "bachelors"
    certificates = ["BSc Computer Systems Engineering"]
    work_history = ["AI Researcher, Harare Institute of Technology (Jan 2026 to date)"]
    achievements = []
    interests = ["artificial intelligence"]
    history = None
    notes = None


CV = """ISAIAH KUZIVAKWASHE CHIKEYA
AI Research Assistant, Harare Institute of Technology, January 2026 to date.
Built the Chord Catcher, a Raspberry Pi guitar chord recogniser using FFT.
Built a Remote Prepaid Water Meter IoT system deployed with Royal Funerals.
Junior Software Developer at Tetisol Private Limited, React.js and Node.js.
"""

TRANSCRIPT = "Module marks: Signals and Systems 74, Embedded Systems 81."


# --------------------------------------------------------------------------
# 1. The applicant's own evidence reaches the draft
# --------------------------------------------------------------------------

def test_the_cv_is_part_of_what_drafting_is_allowed_to_use():
    """The single biggest cause of the complaint: their real projects were
    never in front of the model."""
    dossier = evidence.dossier(_Profile(), [_Doc("cv", CV)])

    assert "Chord Catcher" in dossier
    assert "Remote Prepaid Water Meter" in dossier
    assert "Tetisol" in dossier


def test_stated_facts_come_before_the_documents():
    """What the owner typed is unambiguous; a CV is rich but messy. The clean
    list leads and the documents support it."""
    dossier = evidence.dossier(_Profile(), [_Doc("cv", CV)])

    assert dossier.index("Isaiah Kuzivakwashe Chikeya") < dossier.index("Chord Catcher")


def test_the_cv_outranks_the_transcript_when_space_runs_out():
    dossier = evidence.dossier(_Profile(), [_Doc("transcript", TRANSCRIPT), _Doc("cv", CV)])

    assert dossier.index("Chord Catcher") < dossier.index("Signals and Systems")


def test_a_document_with_no_readable_text_is_simply_absent():
    """A scanned certificate is a normal upload. It must not appear as an
    empty heading implying the applicant submitted nothing."""
    dossier = evidence.dossier(_Profile(), [_Doc("certificate", "   ")])

    assert "Certificate" not in dossier
    assert evidence.has_real_evidence([_Doc("certificate", "  ")]) is False


def test_a_profile_with_no_documents_still_produces_a_usable_dossier():
    dossier = evidence.dossier(_Profile(), [])

    assert "Isaiah Kuzivakwashe Chikeya" in dossier
    assert "BSc Computer Systems Engineering" in dossier


def test_the_dossier_is_bounded():
    """Past a certain size a 3B model loses the middle, and the middle is
    where the work history sits."""
    huge = [_Doc("cv", "x" * 50_000), _Doc("transcript", "y" * 50_000)]

    assert len(evidence.dossier(_Profile(), huge)) <= evidence.MAX_DOSSIER_CHARS + 500


# --------------------------------------------------------------------------
# 2. Each requirement is answered knowing about the others
# --------------------------------------------------------------------------

SPEC = SubmissionSpec(
    document_kind="research proposal",
    sections=[
        SectionSpec(title="Policy Problem Statement", guidance="the challenge"),
        SectionSpec(title="Research Objectives", guidance="what you will achieve"),
        SectionSpec(title="Methodology", guidance="how"),
    ],
    format_rules=FormatRules(max_pages=3.5),
    submit_to="research.development@potraz.zw",
    deadline="3 October 2026",
)
OPP = type("O", (), {"title": "POTRAZ Call for Research Proposals",
                     "summary": "ICT policy research in Zimbabwe"})()


def test_the_prompt_shows_the_whole_application_not_just_this_section():
    prompt = section_drafting.build_section_prompt(
        SPEC.sections[1], SPEC, OPP, "- Name: Isaiah", all_sections=SPEC.sections)

    assert "Policy Problem Statement" in prompt
    assert "Methodology" in prompt
    assert "SECTION 2" in prompt


def test_what_is_already_written_is_shown_so_it_is_not_repeated():
    written = [{"title": "Policy Problem Statement",
                "body": "Zimbabwe's ICT regulatory framework is fragmented."}]

    prompt = section_drafting.build_section_prompt(
        SPEC.sections[1], SPEC, OPP, "- Name: Isaiah",
        all_sections=SPEC.sections, written=written)

    assert "do not repeat this ground" in prompt.lower()
    assert "fragmented" in prompt


def test_the_page_limit_becomes_a_word_budget():
    """3.5 pages across nine sections is about 175 words each. Nine 220-word
    sections overshoot by half a page, which the owner then cuts by hand."""
    tight = SubmissionSpec(format_rules=FormatRules(max_pages=3.5))

    assert section_drafting.words_for_each_section(tight, 9) < 220
    assert section_drafting.words_for_each_section(tight, 2) > 220


def test_a_call_that_states_no_page_limit_gets_the_default():
    assert section_drafting.words_for_each_section(SubmissionSpec(), 5) == 220


def test_a_budget_is_never_so_small_the_section_says_nothing():
    silly = SubmissionSpec(format_rules=FormatRules(max_pages=0.5))

    assert section_drafting.words_for_each_section(silly, 40) >= 110


# --------------------------------------------------------------------------
# 3. The model's own formatting noise is removed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "### Policy Problem Statement\nZimbabwe's framework is fragmented.",
    "Policy Problem Statement\nZimbabwe's framework is fragmented.",
    "**Policy Problem Statement**\nZimbabwe's framework is fragmented.",
    "Policy Problem Statement:\nZimbabwe's framework is fragmented.",
])
def test_a_heading_the_model_added_is_taken_back_off(raw):
    """Small models restate the title about a third of the time. Left in, it
    lands under the real heading and every section appears titled twice."""
    cleaned = section_drafting.strip_leaked_heading(raw, "Policy Problem Statement")

    assert cleaned == "Zimbabwe's framework is fragmented."


def test_the_first_real_sentence_is_never_mistaken_for_a_heading():
    body = "Zimbabwe's ICT regulatory framework is fragmented across agencies."

    assert section_drafting.strip_leaked_heading(body, "Policy Problem Statement") == body


def test_markdown_emphasis_does_not_reach_the_word_document():
    cleaned = section_drafting.strip_leaked_heading(
        "Body text.\n\n## A subheading\nMore text.", "Section")

    assert "##" not in cleaned


# --------------------------------------------------------------------------
# 4. The covering letter is written, not assembled
# --------------------------------------------------------------------------

def test_the_letter_is_addressed_to_whoever_the_call_named():
    prompt = section_drafting.build_cover_letter_prompt(SPEC, OPP, "- Name: Isaiah")

    assert "research.development@potraz.zw" in prompt


def test_the_letter_says_which_call_it_answers():
    prompt = section_drafting.build_cover_letter_prompt(SPEC, OPP, "- Name: Isaiah")

    assert "POTRAZ Call for Research Proposals" in prompt
    assert "I am writing to apply" in prompt  # named as the thing to avoid


def test_a_call_with_no_named_recipient_still_gets_a_letter():
    prompt = section_drafting.build_cover_letter_prompt(
        SubmissionSpec(), OPP, "- Name: Isaiah")

    assert "selection committee" in prompt


def test_the_letter_survives_the_model_being_unreachable():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("model down")

    letter = section_drafting.draft_cover_letter(
        SPEC, OPP, "- Name: Isaiah",
        client=httpx.Client(transport=httpx.MockTransport(down)))

    assert letter == ""


# --------------------------------------------------------------------------
# 5. The form the call tells you to complete
# --------------------------------------------------------------------------

FORM_TEXT = """ZIMBABWE NATIONAL BURSARY APPLICATION FORM

SECTION A: PERSONAL DETAILS
Full name: ______________________
National ID number: _____________

SECTION B: ACADEMIC
1. State the qualification you are applying to study
2. Name of the institution offering the programme

Signature: ____________
"""


def test_the_form_is_picked_out_from_the_other_attachments():
    """A call attaches four things and only one is the form."""
    links = [
        "https://x.org/tender-guidance-notes.pdf",
        "https://x.org/bid-application-form.docx",
        "https://x.org/technical-specification.pdf",
    ]

    assert application_form.likely_form(links) == "https://x.org/bid-application-form.docx"


def test_guidance_notes_are_never_mistaken_for_the_form():
    assert application_form.likely_form(["https://x.org/application-guidance-notes.pdf"]) is None


def test_a_call_with_no_attachments_has_no_form():
    assert application_form.likely_form([]) is None
    assert application_form.likely_form(["https://x.org/logo.png"]) is None


def test_the_questions_on_the_form_are_read_out_in_order():
    questions = application_form.questions_in(FORM_TEXT)

    assert "Full name" in questions
    assert "National ID number" in questions
    assert "State the qualification you are applying to study" in questions
    assert questions.index("Full name") < questions.index("National ID number")


def test_page_furniture_is_not_read_as_a_question():
    assert application_form.questions_in("Page 1 of 4\n\n\n") == []


def test_a_document_that_is_not_a_form_yields_nothing_to_answer():
    prose = "This tender is for the supply of ICT equipment to the ministry. " * 20

    assert application_form.questions_in(prose) == []


def test_a_flat_scan_reports_no_fillable_fields():
    """Most government forms are scans. Saying so is better than pretending
    to have filled one."""
    assert application_form.pdf_form_fields(b"not a pdf at all") == {}
    assert application_form.fill_pdf_form(b"not a pdf at all", {"a": "b"}) is None


def test_answers_are_matched_to_field_names_by_the_words_they_share():
    """Forms name fields anything from "Full Name" to "txtName1"."""
    fields = {"txtFullName": "Full Name", "txtID": "National ID number"}
    answers = {"Full name": "Isaiah Chikeya", "National ID number": "63-123456A00"}

    matched = application_form.match_answers_to_fields(fields, answers)

    assert matched["txtFullName"] == "Isaiah Chikeya"
    assert matched["txtID"] == "63-123456A00"


def test_a_field_nothing_answers_is_left_empty_rather_than_guessed():
    """A guess written into a government form is not a small mistake."""
    fields = {"txtBankSortCode": "Bank sort code"}
    answers = {"Full name": "Isaiah Chikeya"}

    assert application_form.match_answers_to_fields(fields, answers) == {}


# --------------------------------------------------------------------------
# 6. Editing keeps everything the owner can see
# --------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from conftest import sign_up  # noqa: E402
from opportunity_agent import api, models_db  # noqa: E402
from opportunity_agent import db as db_module  # noqa: E402

client = TestClient(api.app)


def _opportunity_with_package(package):
    client.cookies.clear()
    sign_up(client, email="drafter@example.com")
    profile = client.post(
        "/profiles", json={"profile_type": "grant", "display_name": "G"}).json()
    session = db_module.SessionLocal()
    row = models_db.StoredOpportunity(
        profile_id=profile["id"], canonical_url="https://example.org/call",
        payload={"title": "A call"}, match_status="eligible",
        match_score=1, match_reasons={}, package=package)
    session.add(row)
    session.commit()
    row_id = row.id
    session.close()
    return profile, row_id


def test_editing_the_sections_does_not_throw_the_letter_away():
    """The letter lives beside the sections, not in them. An older client
    that sends only sections must not silently wipe it."""
    profile, opp_id = _opportunity_with_package({
        "status": "ready",
        "cover_letter": "Dear Selection Committee,",
        "sections": [{"title": "Objectives", "kind": "section", "body": "Old text."}],
    })

    client.put(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/package",
        json={"sections": [{"title": "Objectives", "body": "My own words."}]})

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements").json()

    assert body["draft"]["cover_letter"] == "Dear Selection Committee,"
    assert body["draft"]["sections"][0]["body"] == "My own words."


def test_the_owner_can_rewrite_the_letter():
    profile, opp_id = _opportunity_with_package({
        "status": "ready", "cover_letter": "Machine wrote this.",
        "sections": [{"title": "Objectives", "body": "x"}]})

    client.put(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/package",
        json={"sections": [{"title": "Objectives", "body": "x"}],
              "cover_letter": "I wrote this myself."})

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements").json()

    assert body["draft"]["cover_letter"] == "I wrote this myself."


def test_a_form_answer_stays_a_form_answer_after_editing():
    """Otherwise the exported document files it under the proposal prose."""
    profile, opp_id = _opportunity_with_package({
        "status": "ready",
        "sections": [{"title": "Full name", "kind": "form_field", "body": "Isaiah"}]})

    client.put(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/package",
        json={"sections": [{"title": "Full name", "kind": "form_field",
                            "body": "Isaiah Kuzivakwashe Chikeya"}]})

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements").json()

    assert body["draft"]["sections"][0]["kind"] == "form_field"


def test_the_requirement_list_reaches_the_page():
    profile, opp_id = _opportunity_with_package({
        "status": "ready",
        "requirements": [
            {"kind": "section", "prompt": "Methodology", "guidance": "",
             "source": "the call", "answerable": True},
            {"kind": "form_field", "prompt": "Full name", "guidance": "",
             "source": "the form", "answerable": True},
        ],
        "form": {"url": "https://example.org/form.docx", "filename": "form.docx"},
        "sections": [],
    })

    body = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/requirements").json()

    assert len(body["draft"]["requirements"]) == 2
    assert body["draft"]["form"]["filename"] == "form.docx"
