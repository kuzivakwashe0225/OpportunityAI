from opportunity_agent.store import OpportunityStore


def test_discovery_run_round_trips_with_counts(tmp_path):
    store = OpportunityStore(path=tmp_path / "store.json")

    record = store.record_run(
        queries=["computer science scholarship Zimbabwe"],
        found=3,
        added=2,
        failures=["https://example.org/bad: timeout"],
    )
    restored = OpportunityStore(path=tmp_path / "store.json")

    assert restored.runs[0].id == record.id
    assert restored.runs[0].found == 3
    assert restored.runs[0].added == 2
    assert restored.runs[0].failures == ["https://example.org/bad: timeout"]
    assert restored.runs[0].completed_at