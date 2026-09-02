from opportunity_agent.sources import SourceRegistry, SourceSpec


def test_registry_returns_only_enabled_scholarship_sources():
    registry = SourceRegistry([
        SourceSpec(name="Scholarships A", url="https://a.example.org", kind="scholarship", enabled=True),
        SourceSpec(name="Disabled", url="https://disabled.example.org", kind="scholarship", enabled=False),
        SourceSpec(name="Jobs", url="https://jobs.example.org", kind="job", enabled=True),
    ])

    sources = registry.enabled(kind="scholarship")

    assert [source.name for source in sources] == ["Scholarships A"]
