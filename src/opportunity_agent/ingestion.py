from .models import Opportunity


def merge_opportunity(first: Opportunity, second: Opportunity) -> Opportunity:
    """Merge two already-deduplicated records while preserving source evidence."""
    sources = list(dict.fromkeys([
        *first.sources,
        first.source,
        *second.sources,
        second.source,
    ]))
    evidence = list(dict.fromkeys([*first.evidence, *second.evidence]))
    deadline = max(
        (value for value in (first.deadline, second.deadline) if value is not None),
        default=None,
    )
    return first.model_copy(update={
        "sources": sources,
        "evidence": evidence,
        "deadline": deadline,
        "eligible_countries": list(dict.fromkeys([
            *first.eligible_countries,
            *second.eligible_countries,
        ])),
        "required_levels": list(dict.fromkeys([
            *first.required_levels,
            *second.required_levels,
        ])),
        "required_fields": list(dict.fromkeys([
            *first.required_fields,
            *second.required_fields,
        ])),
        "required_documents": list(dict.fromkeys([
            *first.required_documents,
            *second.required_documents,
        ])),
        "interests": list(dict.fromkeys([*first.interests, *second.interests])),
    })
