"""All terminal rendering. Nothing here touches the network or the store."""

from __future__ import annotations

from datetime import date

from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .model import Activity, Task

console = Console()

ACCENT = "#3b6ef5"
MUTED = "grey58"
DONE = "green"
URGENT = "red"

# Five states of a progress ring, so a glance tells you roughly how far along an
# assignment is without reading the fraction.
RINGS = ("○", "◔", "◑", "◕", "●")


def ring(fraction: float) -> str:
    if fraction >= 1.0:
        return RINGS[-1]
    return RINGS[min(int(fraction * 4), 3)]


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def long_day(d: date) -> str:
    return f"{d:%A, %B} {_ordinal(d.day)}"


def short_day(d: date) -> str:
    return f"{d:%b} {_ordinal(d.day)}"


def due_style(activity: Activity, today: date | None = None) -> str:
    if activity.complete:
        return DONE
    left = activity.days_left(today)
    if left < 0:
        return URGENT
    if left <= 2:
        return "yellow"
    return ACCENT


def header(activity: Activity, today: date | None = None) -> Panel:
    """The title bar: name, class, due badge, progress ring, date span."""
    today = today or date.today()
    left = activity.days_left(today)

    if activity.complete:
        badge = Text(" Complete ", style=f"bold white on {DONE}")
    elif left < 0:
        badge = Text(f" {abs(left)}d overdue ", style=f"bold white on {URGENT}")
    else:
        badge = Text(f" Due {long_day(activity.due_date)} ", style=f"bold white on {due_style(activity, today)}")

    title = Text()
    title.append(activity.name, style="bold underline")
    if activity.course:
        title.append(f"   {activity.course}", style=MUTED)

    line = Table.grid(expand=True)
    line.add_column(justify="left", ratio=1)
    line.add_column(justify="right")
    line.add_row(title, badge)

    stats = Text()
    stats.append(f"  {ring(activity.fraction)} ", style=due_style(activity, today))
    stats.append(f"{activity.done_count}/{activity.total_count}", style="bold")
    stats.append(
        f"    {short_day(activity.start_date)} → {short_day(activity.due_date)}",
        style=MUTED,
    )
    if activity.attachments:
        stats.append(f"    📎 {len(activity.attachments)}", style=MUTED)

    body = Group(line, Text(""), stats)
    return Panel(body, border_style=MUTED, padding=(0, 1))


def task_row(task: Task, index: int, selected: bool = False) -> Text:
    """One task line: '> [x] 3  Title            10:00 – 11:00'."""
    line = Text()
    line.append("› " if selected else "  ", style=f"bold {ACCENT}")

    if task.done:
        line.append("✔ ", style=f"bold {DONE}")
    else:
        line.append("○ ", style=MUTED)

    line.append(f"{index:>2}  ", style=MUTED)

    title_style = f"{MUTED} strike" if task.done else ("bold" if selected else "")
    line.append(f"{task.title:<44.44}", style=title_style)

    if task.slot:
        line.append(f"  {task.slot}", style=MUTED)
    return line


def task_list(activity: Activity, cursor: int | None = None, show_detail: bool = False) -> Group:
    """Tasks grouped under their day heading, in chronological order."""
    if not activity.tasks:
        return Group(Text("\n  No tasks yet. Run `boost replan <id>` to build a plan.", style=MUTED))

    parts: list = []
    current_day: date | None = None

    for i, task in enumerate(activity.tasks):
        if task.day != current_day:
            current_day = task.day
            parts.append(Text(""))
            heading = Text(f"  {long_day(task.day)}", style="bold")
            if task.day == date.today():
                heading.append("   today", style=f"bold {ACCENT}")
            parts.append(heading)

        parts.append(task_row(task, i + 1, selected=(cursor == i)))

        if show_detail and task.detail and (cursor is None or cursor == i):
            parts.append(Text(f"        {task.detail}", style=MUTED))

    return Group(*parts)


def activity_view(activity: Activity, cursor: int | None = None, show_detail: bool = True) -> Group:
    parts: list = [header(activity)]
    if activity.summary:
        parts.append(Text(f"\n  {activity.summary}", style=f"italic {MUTED}"))
    parts.append(task_list(activity, cursor=cursor, show_detail=show_detail))
    return Group(*parts)


def activity_table(activities: list[Activity], today: date | None = None) -> Table | Text:
    if not activities:
        return Text(
            "\n  Nothing tracked yet. Add your first assignment with `boost add`.\n",
            style=MUTED,
        )

    today = today or date.today()
    table = Table(box=None, padding=(0, 2), expand=False)
    table.add_column("#", style=MUTED, justify="right")
    table.add_column("")
    table.add_column("Activity", style="bold")
    table.add_column("Class", style=MUTED)
    table.add_column("Progress", justify="right")
    table.add_column("Due")

    for a in activities:
        left = a.days_left(today)
        if a.complete:
            due = Text("done", style=DONE)
        elif left < 0:
            due = Text(f"{short_day(a.due_date)}  ({abs(left)}d over)", style=URGENT)
        elif left == 0:
            due = Text(f"{short_day(a.due_date)}  (today)", style=URGENT)
        else:
            due = Text(f"{short_day(a.due_date)}  ({left}d)", style=due_style(a, today))

        table.add_row(
            str(a.id),
            Text(ring(a.fraction), style=due_style(a, today)),
            a.name,
            a.course,
            f"{a.done_count}/{a.total_count}",
            due,
        )
    return table


def agenda(rows: list[tuple[Activity, int]], day: date) -> Group:
    """The `boost today` view: tasks from every activity for one day."""
    heading = Text(f"\n  {long_day(day)}", style="bold")
    if day == date.today():
        heading.append("   today", style=f"bold {ACCENT}")

    if not rows:
        return Group(heading, Text("\n  Nothing scheduled. Enjoy it.\n", style=MUTED))

    parts: list = [heading, Text("")]
    for activity, index in rows:
        task = activity.tasks[index - 1]
        line = Text("  ")
        line.append("✔ " if task.done else "○ ", style=DONE if task.done else MUTED)
        line.append(f"{activity.id}.{index:<4}", style=MUTED)
        line.append(
            f"{task.title:<40.40}",
            style=f"{MUTED} strike" if task.done else "",
        )
        line.append(f"  {task.slot:<14}", style=MUTED)
        line.append(activity.name, style=ACCENT)
        parts.append(line)
    parts.append(Text(""))
    return Group(*parts)


def banner(text: str, style: str = ACCENT) -> Align:
    return Align.left(Text(f"  {text}", style=style))
