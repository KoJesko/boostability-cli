"""JSON-backed persistence with atomic writes."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path

from .config import store_path
from .model import Activity


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or store_path()
        self.activities: list[Activity] = []
        self.next_id: int = 1
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise SystemExit(f"boost: could not read {self.path}: {exc}")
        self.activities = [Activity.from_dict(a) for a in raw.get("activities", [])]
        self.next_id = int(raw.get("next_id", len(self.activities) + 1))

    def save(self) -> None:
        payload = {
            "version": 1,
            "next_id": self.next_id,
            "activities": [a.to_dict() for a in self.activities],
        }
        # Write to a sibling temp file then rename, so an interrupted write can
        # never leave a half-serialized store behind.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def add(self, activity: Activity) -> Activity:
        activity.id = self.next_id
        self.next_id += 1
        self.activities.append(activity)
        return activity

    def get(self, activity_id: int) -> Activity:
        for a in self.activities:
            if a.id == activity_id:
                return a
        raise KeyError(activity_id)

    def remove(self, activity_id: int) -> Activity:
        activity = self.get(activity_id)
        self.activities.remove(activity)
        return activity

    def sorted_activities(self) -> list[Activity]:
        """Soonest due first; finished assignments sink to the bottom."""
        return sorted(self.activities, key=lambda a: (a.complete, a.due_date, a.id))

    def tasks_on(self, day: date) -> list[tuple[Activity, int]]:
        """(activity, 1-based task index) pairs scheduled for `day`."""
        out: list[tuple[Activity, int]] = []
        for activity in self.sorted_activities():
            for i, task in enumerate(activity.tasks, start=1):
                if task.day == day:
                    out.append((activity, i))
        return out
