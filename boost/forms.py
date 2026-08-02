"""The 'Add Your Assignment' flow, as terminal prompts."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

from rich.prompt import Confirm, Prompt
from rich.text import Text

from .model import Activity, ActivityDetails
from .render import ACCENT, MUTED, console

DETAILS_TEMPLATE = """\
My assignment (what I need to do, in details):


When I can study (days/times):


How long I can focus at a time:


Other details that might help:

"""

_SECTIONS = [
    ("assignment", "My assignment (what I need to do, in details):"),
    ("availability", "When I can study (days/times):"),
    ("focus", "How long I can focus at a time:"),
    ("other", "Other details that might help:"),
]


def parse_details_text(text: str) -> ActivityDetails:
    """Split the editor buffer back into its four fields.

    Anything before the first recognised heading is treated as the assignment
    body, so a student who deletes the scaffolding and just writes prose still
    gets something usable.
    """
    positions: list[tuple[int, int, str]] = []
    for i, (key, heading) in enumerate(_SECTIONS):
        match = re.search(re.escape(heading), text, re.I)
        if match:
            positions.append((match.start(), match.end(), key))

    if not positions:
        return ActivityDetails(assignment=text.strip())

    positions.sort()
    values: dict[str, str] = {}
    for i, (_, body_start, key) in enumerate(positions):
        body_end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        values[key] = text[body_start:body_end].strip()

    leading = text[: positions[0][0]].strip()
    if leading and not values.get("assignment"):
        values["assignment"] = leading

    return ActivityDetails(**{k: values.get(k, "") for k, _ in _SECTIONS})


def open_editor(initial: str = DETAILS_TEMPLATE) -> str:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as fh:
        fh.write(initial)
        path = fh.name
    try:
        subprocess.run([*editor.split(), path], check=True)
        return Path(path).read_text()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(f"boost: could not run editor '{editor}': {exc}")
    finally:
        Path(path).unlink(missing_ok=True)


def parse_date(raw: str, *, base: date | None = None) -> date:
    """Accept ISO, US slash format, 'today'/'tomorrow', or '+N' days."""
    base = base or date.today()
    value = raw.strip().lower()

    if value in ("today", "t"):
        return base
    if value in ("tomorrow", "tmr"):
        return base + timedelta(days=1)

    if match := re.fullmatch(r"\+(\d+)", value):
        return base + timedelta(days=int(match.group(1)))

    try:
        return date.fromisoformat(value)
    except ValueError:
        pass

    if match := re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", value):
        month, day, year = (int(g) for g in match.groups())
        if year < 100:
            year += 2000
        return date(year, month, day)

    raise ValueError(
        f"'{raw}' is not a date I understand. Try 2026-08-09, 8/9/2026, "
        f"today, tomorrow, or +7."
    )


def _ask_required(label: str, placeholder: str) -> str:
    console.print(Text(f"\n  {label} ", style="bold").append("*required*", style="red bold"))
    console.print(Text(f"  e.g. {placeholder}", style=MUTED))
    while True:
        value = Prompt.ask("  ›").strip()
        if value:
            return value
        console.print(Text("  This one is required.", style="red"))


def _ask_date(label: str, default: date) -> date:
    console.print(Text(f"\n  {label} ", style="bold").append("*required*", style="red bold"))
    while True:
        raw = Prompt.ask("  ›", default=default.isoformat()).strip()
        try:
            return parse_date(raw)
        except ValueError as exc:
            console.print(Text(f"  {exc}", style="red"))


def _ask_attachments() -> list[str]:
    console.print(Text("\n  Activity attachments", style="bold"))
    console.print(
        Text("  Path to a PDF brief, or blank to skip. Comma-separate several.", style=MUTED)
    )
    raw = Prompt.ask("  ›", default="").strip()
    if not raw:
        return []

    paths: list[str] = []
    for chunk in raw.split(","):
        candidate = Path(chunk.strip()).expanduser()
        if not candidate.exists():
            console.print(Text(f"  Skipping {candidate} — not found.", style="red"))
            continue
        if candidate.suffix.lower() != ".pdf":
            console.print(Text(f"  Skipping {candidate.name} — only PDFs are supported.", style="red"))
            continue
        paths.append(str(candidate.resolve()))
    return paths


def collect_activity(use_editor: bool = False) -> Activity:
    """Run the full add flow and return an Activity with no tasks yet."""
    console.print(Text("\n  Add Your Assignment", style=f"bold {ACCENT}"))
    console.print(
        Text(
            "  Add your assignment and Boostability will turn it into achievable daily tasks.",
            style=MUTED,
        )
    )

    name = _ask_required("Activity name", "Chapter 3 Lab, Midterm Exam, Weekly Quiz")
    course = _ask_required("Class", "BIO101, CHM202, ENG110")

    today = date.today()
    start_date = _ask_date("Activity start date", today)
    while True:
        due_date = _ask_date("Activity due date", start_date + timedelta(days=7))
        if due_date >= start_date:
            break
        console.print(Text("  The due date has to be on or after the start date.", style="red"))

    console.print(Text("\n  Activity details ", style="bold").append("*required*", style="red bold"))
    console.print(
        Text(
            "  Tell me about your assignment. Include when you can study and how\n"
            "  long you can focus. More details = better customized tasks.",
            style=MUTED,
        )
    )

    if use_editor or Confirm.ask("  Open $EDITOR to write these?", default=False):
        details = parse_details_text(open_editor())
    else:
        details = ActivityDetails(
            assignment=Prompt.ask("\n  What you need to do").strip(),
            availability=Prompt.ask("  When you can study (days/times)").strip(),
            focus=Prompt.ask("  How long you can focus at a time").strip(),
            other=Prompt.ask("  Anything else that might help", default="").strip(),
        )

    if not details.assignment:
        raise SystemExit("boost: an assignment description is required.")

    attachments = _ask_attachments()

    return Activity(
        id=0,  # assigned by the store
        name=name,
        course=course,
        start_date=start_date,
        due_date=due_date,
        details=details,
        attachments=attachments,
    )
