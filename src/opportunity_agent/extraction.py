import re
from datetime import date
from urllib.parse import urlparse

from .models import Opportunity
from .search import SearchResult
from .discovery import canonicalize_url
from .connector import PublicPage


def result_to_opportunity(result: SearchResult, *, verified: bool = False) -> Opportunity:
    """Create a conservative draft; parsing requirements is a separate step."""
    canonical_url = canonicalize_url(result.url)
    parsed = urlparse(canonical_url)
    source = parsed.netloc or "unknown source"
    content = result.content
    return Opportunity(
        source=source,
        title=result.title or "Untitled scholarship opportunity",
        url=canonical_url,
        deadline=_extract_deadline(content) if verified else None,
        eligible_countries=_extract_countries(content) if verified else [],
        required_levels=_extract_levels(content) if verified else [],
        required_fields=_extract_fields(content) if verified else [],
        required_age_max=_extract_age(content) if verified else None,
        required_documents=_extract_documents(content) if verified else [],
        evidence=[content] if content else [],
        sources=[canonical_url],
    )


def page_to_opportunity(page: PublicPage, *, title: str) -> Opportunity:
    """Parse fetched public content and retain the retrieval integrity record."""
    opportunity = result_to_opportunity(
        SearchResult(title=title, url=page.url, content=page.content),
        verified=True,
    )
    return opportunity.model_copy(update={
        "retrieved_at": page.retrieved_at,
        "content_sha256": page.sha256,
        "requirements_verified": True,
    })


def _extract_deadline(content: str) -> date | None:
    match = re.search(
        r"(?:deadline|close|closing date|applications? close)[^\d]{0,30}"
        r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})",
        content,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return date(
            int(match.group(3)),
            list_months.index(match.group(2).casefold()) + 1,
            int(match.group(1)),
        )
    except ValueError:
        return None


list_months = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def _extract_countries(content: str) -> list[str]:
    known = ["Zimbabwe", "South Africa", "Botswana", "Zambia", "Mozambique", "Malawi", "Namibia"]
    return [country for country in known if re.search(rf"\b{re.escape(country)}\b", content, re.IGNORECASE)]


def _extract_levels(content: str) -> list[str]:
    levels: list[str] = []
    if re.search(r"master'?s|postgraduate", content, re.IGNORECASE):
        levels.append("masters")
    if re.search(r"undergraduate|bachelor'?s", content, re.IGNORECASE):
        levels.append("undergraduate")
    if re.search(r"doctoral|ph\.?d", content, re.IGNORECASE):
        levels.append("doctoral")
    return levels


def _extract_fields(content: str) -> list[str]:
    known = ["computer science", "engineering", "climate science", "business", "agriculture"]
    return [field for field in known if re.search(rf"\b{re.escape(field)}\b", content, re.IGNORECASE)]


def _extract_age(content: str) -> int | None:
    match = re.search(r"(?:at most|maximum age of|under|below|younger than)\s+(\d{2})", content, re.IGNORECASE)
    if not match:
        return None
    value = int(match.group(1))
    return value - 1 if re.match(r"under|below|younger", match.group(0), re.IGNORECASE) else value


def _extract_documents(content: str) -> list[str]:
    known = ["cv", "transcript", "reference letter", "passport", "statement of purpose"]
    return [document for document in known if re.search(rf"\b{re.escape(document)}\b", content, re.IGNORECASE)]