from urllib.parse import urlparse

from .models import Opportunity
from .search import SearchResult


def result_to_opportunity(result: SearchResult) -> Opportunity:
    """Create a conservative draft; parsing requirements is a separate step."""
    parsed = urlparse(result.url)
    source = parsed.netloc or "unknown source"
    return Opportunity(
        source=source,
        title=result.title or "Untitled scholarship opportunity",
        url=result.url,
        evidence=[result.content] if result.content else [],
        sources=[result.url],
    )