"""Deterministic checks on a generated plan.

The JSON Schema in schema.py constrains the *shape* of what Haiku returns. It
cannot constrain *meaning* -- nothing in a schema stops the model from putting a
task three days after the due date. Date arithmetic is where language models are
weakest, so every invariant that can be checked in code is checked here rather
than trusted to the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .model import Activity, Task

# Issue codes, so callers can branch on a symbol rather than a message string.
AFTER_DUE = "after_due"
BEFORE_START = "before_start"
IN_PAST = "in_past"
BAD_TIMES = "bad_times"
OVER_FOCUS = "over_focus"


@dataclass
class Issue:
    """A problem with one task in a proposed plan."""

    index: int  # 0-based position in the task list
    code: str
    message: str


def inspect_plan(tasks: list[Task], activity: Activity, today: date | None = None) -> list[Issue]:
    """Find every objectively-wrong thing about a proposed plan.

    Only checks invariants that are unambiguous from structured data. It
    deliberately does NOT try to verify the free-text availability window
    ("Aug 4, 6, 8, 9, 10-13, 15, 10AM-4PM") -- parsing that reliably is a
    research project, and a wrong parse would reject good plans.
    """
    today = today or date.today()
    issues: list[Issue] = []
    limit = activity.details.focus_minutes()

    for i, task in enumerate(tasks):
        if task.day > activity.due_date:
            issues.append(Issue(i, AFTER_DUE, f"falls {(task.day - activity.due_date).days}d after the due date"))
        elif task.day < activity.start_date:
            issues.append(Issue(i, BEFORE_START, "falls before the activity start date"))
        elif task.day < today:
            issues.append(Issue(i, IN_PAST, "is scheduled in the past"))

        minutes = task.minutes
        if minutes is not None and minutes <= 0:
            issues.append(Issue(i, BAD_TIMES, "ends at or before it starts"))
        elif minutes is not None and limit and minutes > limit:
            issues.append(
                Issue(i, OVER_FOCUS, f"runs {minutes}min, past the {limit}min focus limit")
            )

    return issues


# ---------------------------------------------------------------------------
# POLICY HOOK -- this is yours to decide. See the note in the README.
# ---------------------------------------------------------------------------
def reconcile_plan(
    tasks: list[Task],
    activity: Activity,
    issues: list[Issue],
) -> tuple[list[Task], list[str]]:
    """Decide what to actually do about the problems `inspect_plan` found.

    Returns (tasks_to_keep, warnings_to_show).

    The shipped behaviour below is the permissive baseline: keep every task,
    surface every problem, and let the student fix it by hand in `boost show`.
    That is a real, defensible choice -- but it is not the only one, and the
    right answer depends on how much you trust the model versus how much you
    want the student interrupted.

    TODO(you): replace the body below with the policy you actually want.
    Three directions worth weighing:

      1. CLAMP  -- pull out-of-range tasks back inside the window (e.g. a task
         after the due date moves to the due date itself). Nothing is lost and
         the plan is always valid, but you can silently stack four tasks onto
         one day and blow past the focus limit you were trying to protect.

      2. DROP   -- discard any task that violates a hard invariant. The plan is
         guaranteed clean, but the student loses work silently, and dropping a
         late task usually means dropping the final deliverable -- the single
         most important item.

      3. RETRY  -- hand the issue list back to Haiku as a follow-up turn and ask
         it to re-plan. Highest quality and it keeps the model's intent intact,
         but it costs a second API round trip and can loop if the constraints
         are genuinely unsatisfiable (e.g. a due date already in the past).

    A hybrid is likely what you want: retry once for AFTER_DUE / IN_PAST, since
    those make the plan actively wrong, and merely warn for OVER_FOCUS, which is
    advisory. `Issue.code` is there so you can branch per-kind.
    """
    warnings = [f"'{tasks[i.index].title}' {i.message}" for i in issues]
    return tasks, warnings
