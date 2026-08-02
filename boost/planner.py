"""The Anthropic call that turns an assignment into daily tasks."""

from __future__ import annotations

import base64
import json
import re
from datetime import date, time
from pathlib import Path

import anthropic

from .config import MAX_PDF_BYTES, MAX_TOKENS, MODEL, THINKING_BUDGET, api_key
from .model import Activity, Task
from .schema import PLAN_SCHEMA
from .validate import inspect_plan, reconcile_plan


class PlanError(Exception):
    """Anything that stopped us producing a usable plan."""


SYSTEM = """\
You are Boostability, a study planner. You take one assignment and break it into \
a small number of concrete daily tasks a student can actually sit down and do.

Rules you must follow:
- Every task is ONE sitting of work with a clear finish line. "Draft section 2" \
is a task; "work on the paper" is not.
- Schedule tasks ONLY on days and at times the student said they are available. \
If they gave no availability, spread the work evenly across the window and use \
reasonable afternoon hours.
- Never make a block longer than the student's stated focus limit. If the work \
needs more time than that, split it across more sittings.
- Every task date must fall on or between the activity's start date and its due \
date, inclusive. Count the days carefully before you answer.
- The final task is the deliverable itself (submit, present, hand in, ship). \
Place it on or before the due date, never after.
- Front-load the work. Leave breathing room before the deadline rather than \
finishing at the last possible hour.
- Write titles and details in the student's own vocabulary, reusing the nouns \
they used for their assignment. Do not invent requirements they did not mention.
- Prefer fewer, meatier tasks over many trivial ones. Three to eight is typical.
"""


def _b64_pdf(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_PDF_BYTES:
        mb = len(data) / 1024 / 1024
        raise PlanError(
            f"{path.name} is {mb:.1f} MB. The API caps a whole request at 32 MB "
            f"and base64 inflates it by a third, so attachments must stay under "
            f"{MAX_PDF_BYTES // 1024 // 1024} MB."
        )
    # b64encode emits a single unbroken line, which is what the API requires.
    return base64.b64encode(data).decode("ascii")


def _build_content(activity: Activity, today: date) -> list[dict]:
    """Documents first, then the instruction text -- the order the API expects."""
    content: list[dict] = []

    for raw in activity.attachments:
        path = Path(raw).expanduser()
        if not path.exists():
            raise PlanError(f"Attachment not found: {path}")
        if path.suffix.lower() != ".pdf":
            raise PlanError(f"Only PDF attachments are supported, got {path.name}")
        content.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": _b64_pdf(path),
                },
                "title": path.name,
            }
        )

    span = (activity.due_date - activity.start_date).days
    text = (
        f"Today is {today:%A, %B %-d, %Y}.\n\n"
        f"Activity name: {activity.name}\n"
        f"Class: {activity.course}\n"
        f"Activity start date: {activity.start_date.isoformat()} "
        f"({activity.start_date:%A})\n"
        f"Activity due date: {activity.due_date.isoformat()} "
        f"({activity.due_date:%A})\n"
        f"Days available: {span + 1}\n\n"
        f"{activity.details.to_prompt()}\n"
    )
    if activity.attachments:
        text += (
            "\nThe attached PDF is the assignment brief. Use its actual "
            "requirements and deadlines; where it conflicts with the notes "
            "above, the PDF wins.\n"
        )
    text += "\nBreak this into daily tasks."

    content.append({"type": "text", "text": text})
    return content


_TIME_PATTERNS = (
    # 10:00, 14:30
    (re.compile(r"^(\d{1,2}):(\d{2})$"), lambda m: (int(m[1]), int(m[2]))),
    # 10:00 AM, 2:30pm
    (
        re.compile(r"^(\d{1,2}):(\d{2})\s*([ap])\.?m\.?$", re.I),
        lambda m: (_h12(int(m[1]), m[3]), int(m[2])),
    ),
    # 10AM, 2 pm
    (re.compile(r"^(\d{1,2})\s*([ap])\.?m\.?$", re.I), lambda m: (_h12(int(m[1]), m[2]), 0)),
    # bare hour
    (re.compile(r"^(\d{1,2})$"), lambda m: (int(m[1]), 0)),
)


def _h12(hour: int, meridiem: str) -> int:
    hour = hour % 12
    return hour + 12 if meridiem.lower() == "p" else hour


def _norm_time(value: str | None) -> time | None:
    """Coerce whatever the model wrote into a time, or None if unusable.

    The schema cannot express an HH:MM pattern (structured outputs reject
    `pattern`), so the model occasionally returns '2:30 PM' or '14'. Normalizing
    here beats failing the whole plan over a formatting slip.
    """
    if not value:
        return None
    raw = value.strip()
    for pattern, extract in _TIME_PATTERNS:
        match = pattern.match(raw)
        if match:
            hour, minute = extract(match)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return time(hour, minute)
            return None
    return None


def _extract_json(message) -> dict:
    """Pull the structured payload out of the response.

    With --think the response leads with thinking blocks, so we scan for the
    first text block rather than assuming content[0].
    """
    if message.stop_reason == "refusal":
        raise PlanError(
            "Claude declined to plan this assignment. Rephrasing the details "
            "usually resolves it."
        )
    if message.stop_reason == "max_tokens":
        raise PlanError(
            "The plan was cut off before it finished. Try a shorter details "
            "field, or raise MAX_TOKENS in boost/config.py."
        )

    for block in message.content:
        if block.type == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError as exc:
                raise PlanError(f"Model returned unparseable JSON: {exc}") from exc

    raise PlanError("Model returned no text content.")


def generate_plan(
    activity: Activity,
    *,
    think: bool = False,
    today: date | None = None,
) -> tuple[list[Task], str, list[str]]:
    """Ask Haiku for a plan. Returns (tasks, summary, warnings)."""
    today = today or date.today()

    if activity.due_date < activity.start_date:
        raise PlanError("The due date is before the start date.")

    client = anthropic.Anthropic(api_key=api_key())

    request = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": _build_content(activity, today)}],
        # Constrains generation itself, so the model cannot emit a shape that
        # fails to parse -- no markdown fences, no trailing prose.
        "output_config": {"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
    }
    if think:
        # Haiku 4.5 predates adaptive thinking: it takes an explicit token
        # budget, which must stay below max_tokens. Passing `effort` here -- the
        # newer control used by Opus/Sonnet 5 -- would be a 400.
        request["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}

    try:
        message = client.messages.create(**request)
    except anthropic.NotFoundError as exc:
        raise PlanError(f"Model '{MODEL}' is not available to this key: {exc}") from exc
    except anthropic.AuthenticationError as exc:
        raise PlanError(f"API key rejected: {exc}") from exc
    except anthropic.RateLimitError as exc:
        raise PlanError(f"Rate limited -- wait a moment and retry. ({exc})") from exc
    except anthropic.APIStatusError as exc:
        raise PlanError(f"API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise PlanError(f"Could not reach the API: {exc}") from exc

    payload = _extract_json(message)

    tasks: list[Task] = []
    for raw in payload.get("tasks", []):
        try:
            day = date.fromisoformat(raw["date"])
        except (KeyError, ValueError):
            continue  # a task with no usable date is not a task
        tasks.append(
            Task(
                title=raw.get("title", "Untitled task").strip(),
                day=day,
                start=_norm_time(raw.get("start_time")),
                end=_norm_time(raw.get("end_time")),
                detail=raw.get("detail", "").strip(),
            )
        )

    if not tasks:
        raise PlanError("The model returned an empty plan.")

    tasks.sort(key=lambda t: (t.day, t.start or time.min))

    issues = inspect_plan(tasks, activity, today=today)
    tasks, warnings = reconcile_plan(tasks, activity, issues)

    return tasks, payload.get("summary", "").strip(), warnings
