import json

from opportunity_agent.models import PersonalProfile
from opportunity_agent.storage import ProfileStore


def make_profile(**overrides):
    values = {
        "name": "Test Applicant",
        "country": "Zimbabwe",
        "age": 29,
        "study_level": "masters",
        "field": "Computer Science",
        "documents": ["transcript", "cv"],
        "interests": ["technology"],
    }
    values.update(overrides)
    return PersonalProfile(**values)


def test_save_then_load_round_trips_profile(tmp_path):
    store = ProfileStore(tmp_path / "profile.json")

    store.save(make_profile())
    loaded = store.load()

    assert loaded == make_profile()


def test_load_without_a_saved_profile_returns_none(tmp_path):
    store = ProfileStore(tmp_path / "profile.json")

    assert store.load() is None


def test_save_overwrites_the_previous_profile(tmp_path):
    store = ProfileStore(tmp_path / "profile.json")

    store.save(make_profile(age=29))
    store.save(make_profile(age=30))

    assert store.load().age == 30


def test_saved_file_is_readable_json(tmp_path):
    path = tmp_path / "profile.json"
    store = ProfileStore(path)

    store.save(make_profile())

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["name"] == "Test Applicant"


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "vault" / "profile.json"
    store = ProfileStore(path)

    store.save(make_profile())

    assert path.exists()
