"""Command dispatch."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from rich.text import Text

from . import __version__
from .config import MODEL, MissingCredentials, store_path
from .export import FORMATS
from .forms import collect_activity, parse_date
from .model import Activity
from .planner import PlanError, generate_plan
from .render import ACCENT, MUTED, URGENT, activity_table, activity_view, agenda, console
from .store import Store
from .tui import run_checklist


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _fail(message: str) -> int:
    console.print(Text(f"\nboost: {message}\n", style=URGENT))
    return 1


def _get_activity(store: Store, activity_id: int) -> Activity:
    try:
        return store.get(activity_id)
    except KeyError:
        raise SystemExit(
            f"boost: no activity with id {activity_id}. Run `boost list` to see them."
        )


def _parse_ref(ref: str) -> tuple[int, int]:
    """'3.2' -> (activity 3, 1-based task 2)."""
    if "." not in ref:
        raise SystemExit(f"boost: '{ref}' should look like 3.2 (activity.task).")
    left, _, right = ref.partition(".")
    try:
        return int(left), int(right)
    except ValueError:
        raise SystemExit(f"boost: '{ref}' should look like 3.2 (activity.task).")


def _plan_into(activity: Activity, think: bool) -> list[str]:
    """Run the model and attach the resulting tasks. Returns warnings."""
    label = f"Breaking it down with {MODEL}"
    if activity.attachments:
        label += f" (+{len(activity.attachments)} PDF)"
    with console.status(f"[{ACCENT}]{label}…", spinner="dots"):
        tasks, summary, warnings = generate_plan(activity, think=think)
    activity.tasks = tasks
    activity.summary = summary
    activity.sort_tasks()
    return warnings


def _show_warnings(warnings: list[str]) -> None:
    if not warnings:
        return
    console.print(Text("\n  Check these before you commit to the plan:", style="yellow bold"))
    for warning in warnings:
        console.print(Text(f"    • {warning}", style="yellow"))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_add(args: argparse.Namespace) -> int:
    store = Store()
    activity = collect_activity(use_editor=args.editor)

    if not args.no_plan:
        try:
            warnings = _plan_into(activity, args.think)
        except (PlanError, MissingCredentials) as exc:
            # The assignment is still worth keeping even if planning failed --
            # `boost replan <id>` can retry once the problem is fixed.
            store.add(activity)
            store.save()
            console.print(
                Text(f"\n  Saved as activity {activity.id}, but planning failed:", style="yellow")
            )
            return _fail(f"{exc}\n\n  Retry with: boost replan {activity.id}")
    else:
        warnings = []

    store.add(activity)
    store.save()

    console.print()
    console.print(activity_view(activity))
    _show_warnings(warnings)
    console.print(
        Text(f"\n  Saved as activity {activity.id}. ", style=ACCENT).append(
            f"Open it with `boost show {activity.id}`.\n", style=MUTED
        )
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = Store()
    activities = store.sorted_activities()
    if not args.all:
        activities = [a for a in activities if not a.complete] or activities
    console.print()
    console.print(activity_table(activities))
    console.print()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = Store()
    activity = _get_activity(store, args.id)

    if args.static:
        console.print()
        console.print(activity_view(activity, show_detail=True))
        console.print()
        return 0

    if run_checklist(activity):
        activity.sort_tasks()
        store.save()
        console.print()
        console.print(activity_view(activity, show_detail=False))
        console.print(
            Text(f"\n  Saved. {activity.done_count}/{activity.total_count} done.\n", style=ACCENT)
        )
    return 0


def cmd_today(args: argparse.Namespace) -> int:
    store = Store()
    start = parse_date(args.date) if args.date else date.today()
    console.print()
    for offset in range(max(args.days, 1)):
        day = start + timedelta(days=offset)
        console.print(agenda(store.tasks_on(day), day))
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    return _set_done(args, True)


def cmd_undo(args: argparse.Namespace) -> int:
    return _set_done(args, False)


def _set_done(args: argparse.Namespace, value: bool) -> int:
    store = Store()
    touched: list[Activity] = []

    for ref in args.refs:
        activity_id, index = _parse_ref(ref)
        activity = _get_activity(store, activity_id)
        if not 1 <= index <= len(activity.tasks):
            return _fail(f"activity {activity_id} has no task {index}.")
        task = activity.tasks[index - 1]
        task.done = value
        touched.append(activity)
        mark = "✔" if value else "○"
        style = "green" if value else MUTED
        console.print(
            Text(f"\n  {mark} {ref}  ", style=style)
            .append(task.title, style="bold")
            .append(f"    ({activity.done_count}/{activity.total_count})", style=MUTED)
        )

    store.save()
    for activity in touched:
        if activity.complete:
            console.print(
                Text(f"  🎉 {activity.name} is complete.", style="bold green")
            )
    console.print()
    return 0


def cmd_replan(args: argparse.Namespace) -> int:
    store = Store()
    activity = _get_activity(store, args.id)

    if activity.tasks and not args.force:
        console.print(
            Text(
                f"\n  Activity {activity.id} already has {activity.total_count} tasks "
                f"({activity.done_count} done). Replanning replaces all of them.",
                style="yellow",
            )
        )
        console.print(Text("  Re-run with --force to go ahead.\n", style=MUTED))
        return 1

    try:
        warnings = _plan_into(activity, args.think)
    except (PlanError, MissingCredentials) as exc:
        return _fail(str(exc))

    store.save()
    console.print()
    console.print(activity_view(activity))
    _show_warnings(warnings)
    console.print()
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    store = Store()
    activity = _get_activity(store, args.id)
    if not args.force:
        console.print(
            Text(f"\n  This deletes “{activity.name}” and its {activity.total_count} tasks.", style="yellow")
        )
        console.print(Text("  Re-run with --force to confirm.\n", style=MUTED))
        return 1
    store.remove(args.id)
    store.save()
    console.print(Text(f"\n  Deleted “{activity.name}”.\n", style=MUTED))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = Store()
    activity = _get_activity(store, args.id)
    text = FORMATS[args.format](activity)

    if args.output:
        Path(args.output).write_text(text)
        console.print(Text(f"\n  Wrote {args.output}\n", style=ACCENT))
    else:
        # Bypass rich so the output stays byte-exact and pipeable.
        sys.stdout.write(text)
    return 0


def cmd_where(args: argparse.Namespace) -> int:
    console.print(Text(f"\n  store   {store_path()}", style=MUTED))
    console.print(Text(f"  model   {MODEL}\n", style=MUTED))
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boost",
        description="Turn assignments into achievable daily tasks.",
    )
    parser.add_argument("--version", action="version", version=f"boost {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="add an assignment and break it down")
    p_add.add_argument("--editor", action="store_true", help="write details in $EDITOR")
    p_add.add_argument("--think", action="store_true", help="let the model reason before planning")
    p_add.add_argument("--no-plan", action="store_true", help="save without calling the API")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", aliases=["ls"], help="list tracked activities")
    p_list.add_argument("-a", "--all", action="store_true", help="include completed")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="open an activity's checklist")
    p_show.add_argument("id", type=int)
    p_show.add_argument("--static", action="store_true", help="print instead of going interactive")
    p_show.set_defaults(func=cmd_show)

    p_today = sub.add_parser("today", help="what's scheduled today")
    p_today.add_argument("-d", "--days", type=int, default=1, help="how many days to show")
    p_today.add_argument("--date", help="start from this date instead of today")
    p_today.set_defaults(func=cmd_today)

    p_done = sub.add_parser("done", help="tick tasks off, e.g. boost done 3.2")
    p_done.add_argument("refs", nargs="+", metavar="ACTIVITY.TASK")
    p_done.set_defaults(func=cmd_done)

    p_undo = sub.add_parser("undo", help="un-tick tasks")
    p_undo.add_argument("refs", nargs="+", metavar="ACTIVITY.TASK")
    p_undo.set_defaults(func=cmd_undo)

    p_replan = sub.add_parser("replan", help="regenerate the plan for an activity")
    p_replan.add_argument("id", type=int)
    p_replan.add_argument("--think", action="store_true")
    p_replan.add_argument("-f", "--force", action="store_true")
    p_replan.set_defaults(func=cmd_replan)

    p_rm = sub.add_parser("rm", help="delete an activity")
    p_rm.add_argument("id", type=int)
    p_rm.add_argument("-f", "--force", action="store_true")
    p_rm.set_defaults(func=cmd_rm)

    p_export = sub.add_parser("export", help="export a plan")
    p_export.add_argument("id", type=int)
    p_export.add_argument("--format", choices=sorted(FORMATS), default="md")
    p_export.add_argument("-o", "--output")
    p_export.set_defaults(func=cmd_export)

    sub.add_parser("where", help="show config and store paths").set_defaults(func=cmd_where)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        # Bare `boost` shows the dashboard rather than a usage dump -- it is the
        # thing you actually want 90% of the time.
        store = Store()
        console.print()
        console.print(activity_table(store.sorted_activities()))
        console.print(agenda(store.tasks_on(date.today()), date.today()))
        console.print(Text("  boost add    boost show <id>    boost today\n", style=MUTED))
        return 0

    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print(Text("\n  Cancelled.\n", style=MUTED))
        return 130
    except MissingCredentials as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
