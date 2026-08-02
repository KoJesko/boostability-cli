"""Render an activity out to Markdown or iCalendar."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from .model import Activity
from .render import long_day


def to_markdown(activity: Activity) -> str:
    lines = [
        f"# {activity.name}",
        "",
        f"**Class:** {activity.course}  ",
        f"**Window:** {activity.start_date.isoformat()} → {activity.due_date.isoformat()}  ",
        f"**Progress:** {activity.done_count}/{activity.total_count}",
        "",
    ]
    if activity.summary:
        lines += [f"> {activity.summary}", ""]

    current: date | None = None
    for task in activity.tasks:
        if task.day != current:
            current = task.day
            lines += [f"## {long_day(task.day)}", ""]
        box = "x" if task.done else " "
        slot = f" _{task.slot}_" if task.slot else ""
        lines.append(f"- [{box}] **{task.title}**{slot}")
        if task.detail:
            lines.append(f"      {task.detail}")
    lines.append("")
    return "\n".join(lines)


def _esc(text: str) -> str:
    """iCalendar TEXT escaping, per RFC 5545 §3.3.11."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 requires content lines wrap at 75 octets with a leading space."""
    if len(line) <= 75:
        return line
    chunks = [line[:75]]
    rest = line[75:]
    while rest:
        chunks.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(chunks)


def to_ics(activity: Activity) -> str:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Boostability//boost-cli//EN",
        "CALSCALE:GREGORIAN",
    ]

    for i, task in enumerate(activity.tasks, start=1):
        uid = f"boost-{activity.id}-{i}@boostability.local"
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}Z"]

        if task.start and task.end:
            # Floating local time: no timezone suffix, so the event lands at the
            # same wall-clock hour wherever the calendar is opened.
            lines.append(f"DTSTART:{task.day:%Y%m%d}T{task.start:%H%M%S}")
            lines.append(f"DTEND:{task.day:%Y%m%d}T{task.end:%H%M%S}")
        else:
            lines.append(f"DTSTART;VALUE=DATE:{task.day:%Y%m%d}")
            lines.append(f"DTEND;VALUE=DATE:{task.day + timedelta(days=1):%Y%m%d}")

        lines.append(f"SUMMARY:{_esc(task.title)}")
        description = task.detail or ""
        if activity.course:
            description = f"[{activity.course}] {description}".strip()
        if description:
            lines.append(f"DESCRIPTION:{_esc(description)}")
        lines.append(f"STATUS:{'COMPLETED' if task.done else 'CONFIRMED'}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def to_json(activity: Activity) -> str:
    return json.dumps(activity.to_dict(), indent=2)


FORMATS = {"md": to_markdown, "ics": to_ics, "json": to_json}
