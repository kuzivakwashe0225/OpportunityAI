"""Turn matched opportunities into a human-readable digest.

This is the review-workspace surface from SOLUTION_DEFINITION.md §5.7: it never
submits anything, it only tells the owner what showed up and why. Ineligible
opportunities are dropped silently — surfacing them would just add noise the
owner has to read past on every digest.
"""

from datetime import date

from .models import MatchResult, Opportunity

_ELIGIBLE_HEADING = "Ready to apply"
_NEEDS_REVIEW_HEADING = "Needs review"

Pair = tuple[Opportunity, MatchResult]


def build_digest(results: list[Pair]) -> str:
    eligible = [pair for pair in results if pair[1].status == "eligible"]
    needs_review = [pair for pair in results if pair[1].status == "needs_review"]

    if not eligible and not needs_review:
        return "No new scholarship matches today."

    sections: list[str] = []

    if eligible:
        eligible.sort(key=lambda pair: (-pair[1].score, pair[0].deadline or date.max))
        sections.append(_section(_ELIGIBLE_HEADING, eligible, "Matches"))

    if needs_review:
        needs_review.sort(key=lambda pair: pair[0].deadline or date.max)
        sections.append(_section(_NEEDS_REVIEW_HEADING, needs_review, "Missing"))

    return "\n\n".join(sections)


def _section(heading: str, pairs: list[Pair], reason_label: str) -> str:
    lines = [f"## {heading} ({len(pairs)})", ""]
    for opportunity, result in pairs:
        reasons = result.matched_requirements if reason_label == "Matches" else result.unknown_requirements
        lines.append(_entry(opportunity, result, reasons, reason_label))
    return "\n".join(lines)


def _entry(opportunity: Opportunity, result: MatchResult, reasons: list[str], label: str) -> str:
    deadline = opportunity.deadline.isoformat() if opportunity.deadline else "no deadline given"
    reason_text = "; ".join(reasons) if reasons else "no details recorded"
    return (
        f"- **{opportunity.title}** ({opportunity.source}) "
        f"— deadline {deadline}, score {result.score}\n"
        f"  {label}: {reason_text}\n"
        f"  {opportunity.url}"
    )
