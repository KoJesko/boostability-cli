"""Domain objects: an Activity is an assignment; Tasks are its daily steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    return time.fromisoformat(value)


@dataclass
class Task:
    """One sitting of work on a specific day."""

    title: str
    day: date
    start: time | None = None
    end: time | None = None
    detail: str = ""
    done: bool = False

    @property
    def slot(self) -> str:
        if self.start and self.end:
            return f"{self.start.strftime('%H:%M')} – {self.end.strftime('%H:%M')}"
        if self.start:
            return self.start.strftime("%H:%M")
        return ""

    @property
    def minutes(self) -> int | None:
        """Length of the block, or None if it has no bounded time range."""
        if not (self.start and self.end):
            return None
        start_m = self.start.hour * 60 + self.start.minute
        end_m = self.end.hour * 60 + self.end.minute
        return end_m - start_m

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "day": self.day.isoformat(),
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "detail": self.detail,
            "done": self.done,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Task":
        return cls(
            title=raw["title"],
            day=_parse_date(raw["day"]),
            start=_parse_time(raw.get("start")),
            end=_parse_time(raw.get("end")),
            detail=raw.get("detail", ""),
            done=bool(raw.get("done", False)),
        )


@dataclass
class ActivityDetails:
    """The four free-text fields from the assignment form."""

    assignment: str = ""
    availability: str = ""
    focus: str = ""
    other: str = ""

    def to_prompt(self) -> str:
        return (
            f"My assignment (what I need to do, in detail):\n{self.assignment or '(not given)'}\n\n"
            f"When I can study (days/times):\n{self.availability or '(not given)'}\n\n"
            f"How long I can focus at a time:\n{self.focus or '(not given)'}\n\n"
            f"Other details that might help:\n{self.other or '(none)'}"
        )

    def focus_minutes(self) -> int | None:
        """Best-effort minutes parsed from the free-text focus field.

        Returns None when the text does not clearly state a duration -- callers
        must treat that as "no constraint", never as zero.
        """
        import re

        text = self.focus.lower()
        hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:h\b|hr|hour)", text)
        if hours:
            return int(float(hours.group(1)) * 60)
        mins = re.search(r"(\d+)\s*(?:m\b|min)", text)
        if mins:
            return int(mins.group(1))
        return None

    def to_dict(self) -> dict:
        return {
            "assignment": self.assignment,
            "availability": self.availability,
            "focus": self.focus,
            "other": self.other,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ActivityDetails":
        return cls(
            assignment=raw.get("assignment", ""),
            availability=raw.get("availability", ""),
            focus=raw.get("focus", ""),
            other=raw.get("other", ""),
        )


@dataclass
class Activity:
    """An assignment plus the plan generated for it."""

    id: int
    name: str
    course: str
    start_date: date
    due_date: date
    details: ActivityDetails = field(default_factory=ActivityDetails)
    attachments: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    summary: str = ""
    created_at: str = ""

    @property
    def done_count(self) -> int:
        return sum(1 for t in self.tasks if t.done)

    @property
    def total_count(self) -> int:
        return len(self.tasks)

    @property
    def complete(self) -> bool:
        return self.total_count > 0 and self.done_count == self.total_count

    @property
    def fraction(self) -> float:
        if not self.tasks:
            return 0.0
        return self.done_count / self.total_count

    def days_left(self, today: date | None = None) -> int:
        return (self.due_date - (today or date.today())).days

    def sort_tasks(self) -> None:
        """Chronological order. Tasks without a start time sort first in a day."""
        self.tasks.sort(key=lambda t: (t.day, t.start or time.min))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "course": self.course,
            "start_date": self.start_date.isoformat(),
            "due_date": self.due_date.isoformat(),
            "details": self.details.to_dict(),
            "attachments": self.attachments,
            "tasks": [t.to_dict() for t in self.tasks],
            "summary": self.summary,
            "created_at": self.created_at or datetime.now().isoformat(timespec="seconds"),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Activity":
        return cls(
            id=int(raw["id"]),
            name=raw["name"],
            course=raw.get("course", ""),
            start_date=_parse_date(raw["start_date"]),
            due_date=_parse_date(raw["due_date"]),
            details=ActivityDetails.from_dict(raw.get("details", {})),
            attachments=list(raw.get("attachments", [])),
            tasks=[Task.from_dict(t) for t in raw.get("tasks", [])],
            summary=raw.get("summary", ""),
            created_at=raw.get("created_at", ""),
        )
