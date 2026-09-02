from opportunity_agent.models import PersonalProfile


def test_profile_carries_certificates_and_work_history_for_discovery():
    profile = PersonalProfile(
        name="Test Applicant",
        certificates=["BSc Computer Science, University of Zimbabwe (2021)"],
        work_history=["Software Engineer, Acme Corp (2021-2024): built payments infrastructure"],
    )

    assert profile.certificates == ["BSc Computer Science, University of Zimbabwe (2021)"]
    assert profile.work_history == [
        "Software Engineer, Acme Corp (2021-2024): built payments infrastructure"
    ]


def test_certificates_and_work_history_default_to_empty():
    profile = PersonalProfile(name="Test Applicant")

    assert profile.certificates == []
    assert profile.work_history == []
