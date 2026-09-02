from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    name: str
    url: str
    kind: str
    enabled: bool = True


class SourceRegistry:
    def __init__(self, sources: list[SourceSpec] | None = None) -> None:
        self._sources = list(sources or [])

    def enabled(self, *, kind: str | None = None) -> list[SourceSpec]:
        return [
            source
            for source in self._sources
            if source.enabled and (kind is None or source.kind == kind)
        ]

    def add(self, source: SourceSpec) -> None:
        if any(existing.url == source.url for existing in self._sources):
            raise ValueError("source URL already registered")
        self._sources.append(source)
