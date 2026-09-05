"""What still stands between a drafted application and a submitted one.

The agent can find an opportunity, decide the owner qualifies, and write the
application. What it cannot do is lodge a bid bond or produce a tax clearance
certificate. So the last useful thing it can do unattended is tell the owner
*precisely* what is left, in the words they will need when they go looking for
it - and separate the things that merely need doing from the things that make
the submission impossible.

Two categories, deliberately distinct:

  blockers  submission cannot succeed until this changes, and some of them the
            owner cannot change at all (wrong supplier category, closed tender)
  actions   concrete work the owner can do now - upload this, pay that, re-read
            the addenda

Hard rule: every line traces to a published fact or a field the owner entered.
"Arrange bid security of 25000" is only produced because the tender page says
25000. Inventing a plausible requirement is worse than silence here, because
the owner would go and act on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from . import profile_schema
from .egp import TenderDetails
from .models import Opportunity

# Far enough out that a bid bond and a tax clearance can still be arranged;
# closer than this and the owner needs to know today.
URGENT_DAYS = 14


@dataclass(frozen=True)
class ComplianceItem:
    key: str
    label: str
    status: Literal["held", "missing"]


@dataclass
class ComplianceReport:
    ready_to_submit: bool
    items: list[ComplianceItem] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ready_to_submit": self.ready_to_submit,
            "items": [{"key": i.key, "label": i.label, "status": i.status} for i in self.items],
            "actions": list(self.actions),
            "blockers": list(self.blockers),
        }


def _money(amount: float) -> str:
    return f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"


def build_report(
    *,
    profile_type: str,
    fields: dict | None,
    held_documents: set[str],
    opportunity: Opportunity,
    details: TenderDetails | None = None,
    as_of: date | None = None,
) -> ComplianceReport:
    fields = fields or {}
    today = as_of or date.today()
    actions: list[str] = []
    blockers: list[str] = []

    # --- documents -------------------------------------------------------
    # Named from the profile schema, not from the raw key: the owner is going
    # to a filing cabinet, and "tax_clearance" is not written on anything in it.
    required = list(opportunity.required_documents)
    items = [
        ComplianceItem(
            key=key,
            label=profile_schema.document_label(profile_type, fields, key),
            status="held" if key.casefold() in {d.casefold() for d in held_documents} else "missing",
        )
        for key in required
    ]
    missing = [item for item in items if item.status == "missing"]
    if missing:
        actions.append(
            "Upload " + ("this document" if len(missing) == 1 else f"these {len(missing)} documents")
            + ": " + ", ".join(item.label for item in missing) + "."
        )

    # --- supplier category ----------------------------------------------
    if opportunity.required_categories:
        held_categories = {
            str(code).strip().upper()
            for code in (fields.get("praz_categories") or fields.get("categories") or [])
            if str(code).strip()
        }
        wanted = {code.strip().upper() for code in opportunity.required_categories}
        if not held_categories:
            actions.append(
                "Add your PRAZ supplier category codes to the profile - this tender requires "
                + ", ".join(sorted(wanted)) + "."
            )
        elif not held_categories & wanted:
            # Not fixable by uploading anything: you either hold the
            # registration or you apply to PRAZ for it, which takes weeks.
            blockers.append(
                "Your PRAZ registration ("
                + ", ".join(sorted(held_categories))
                + ") does not cover this tender, which requires "
                + ", ".join(sorted(wanted)) + "."
            )

    # --- deadline --------------------------------------------------------
    if opportunity.deadline:
        days_left = (opportunity.deadline - today).days
        if days_left < 0:
            blockers.append(f"This closed on {opportunity.deadline.isoformat()}.")
        elif days_left <= URGENT_DAYS:
            actions.append(
                f"Closes in {days_left} days ({opportunity.deadline.isoformat()}) - "
                "anything that has to be arranged through a bank or ZIMRA needs starting now."
            )

    # --- money and terms, only where the tender actually published them ---
    if details is not None:
        # Tenders publish a domestic and an international figure. A bidder pays
        # one of them, so quoting both is noise that makes the owner work out
        # which applies - and the profile already says where they are. Only
        # fall back to showing both when we genuinely don't know.
        country = (fields.get("country") or "").strip().casefold()
        domestic = country == "zimbabwe" if country else None
        money: list[tuple[str, float | None]] = []
        if domestic is not False:
            money.append(("bid security", details.bid_security_domestic))
            money.append(("the tender establishment fee", details.establishment_domestic))
        if domestic is not True:
            money.append((
                "bid security" if domestic is False else "bid security (international bidders)",
                details.bid_security_international,
            ))
            money.append((
                "the tender establishment fee" if domestic is False
                else "the tender establishment fee (international)",
                details.establishment_international,
            ))
        money += [("the SPOC fee", details.spoc_fee), ("the bid form fee", details.bid_form_fee)]

        for label, amount in money:
            # A published zero is a real answer - "nothing to pay" - not an
            # action. Only non-zero amounts are work.
            if amount:
                actions.append(f"Arrange {label} of {_money(amount)}.")

        if details.addendum_count:
            actions.append(
                f"Read the {details.addendum_count} addend"
                + ("um" if details.addendum_count == 1 else "a")
                + " before bidding - they supersede the published requirements."
            )
        if details.bid_validity_days:
            actions.append(
                f"Your bid must stay valid for {details.bid_validity_days} days."
            )
        if details.documents_require_login:
            actions.append(
                "The full bid pack is only downloadable from a logged-in PRAZ eGP "
                "supplier account - it is not on the public listing."
            )

    return ComplianceReport(
        ready_to_submit=not blockers and not missing,
        items=items,
        actions=actions,
        blockers=blockers,
    )
