"""Local persistence for the single-user MVP profile.

MVP scope only (SOLUTION_DEFINITION.md §10): one person, one JSON file on disk.
This is deliberately not a database — swap ProfileStore's insides for
SQLite/Postgres later without changing its save()/load() contract once there's
more than one profile to track (§4's company profile, or multiple users).
"""

from __future__ import annotations

from pathlib import Path

from .models import PersonalProfile


class ProfileStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def save(self, profile: PersonalProfile) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")

    def load(self) -> PersonalProfile | None:
        if not self._path.exists():
            return None
        return PersonalProfile.model_validate_json(self._path.read_text(encoding="utf-8"))
