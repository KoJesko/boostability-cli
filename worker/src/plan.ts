/**
 * The planning contract: schema, prompt, and the deterministic checks that run
 * after the model answers.
 *
 * Ported from the Python reference implementation. The JSON Schema constrains
 * the *shape* of Haiku's output; `inspectPlan` constrains its *meaning*. Date
 * arithmetic is where language models are weakest, so anything checkable in
 * code is checked in code rather than trusted to the prompt.
 */

// Haiku 4.5 is the smallest model supporting both structured outputs and PDF
// input. It is a 200K-context model, so PDFs cap at 100 pages, and it predates
// adaptive thinking -- it takes {type:"enabled",budget_tokens} and rejects the
// `effort` parameter used by Opus/Sonnet 5 with a 400.
export const MODEL = "claude-haiku-4-5";
export const MAX_TOKENS = 8000;
export const THINKING_BUDGET = 2000; // must stay below MAX_TOKENS

export const SYSTEM = `You are Boostability, a study planner. You take one assignment and break it into \
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
- Prefer fewer, meatier tasks over many trivial ones. Three to eight is typical.`;

/**
 * Structured outputs accept only a subset of JSON Schema: no `pattern`, no
 * length or numeric bounds, no recursion, and every object must declare
 * `additionalProperties: false`. Times are therefore plain strings with a
 * descriptive hint, normalized after the fact by `normalizeTime`.
 */
export const PLAN_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["summary", "tasks"],
  properties: {
    summary: {
      type: "string",
      description:
        "One or two sentences addressed to the student, explaining the shape of the plan and any assumption you had to make.",
    },
    tasks: {
      type: "array",
      description:
        "The daily tasks, in chronological order. Usually 3-8; scale with the size of the assignment.",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["title", "date", "start_time", "end_time", "detail"],
        properties: {
          title: {
            type: "string",
            description:
              "Short imperative name for the sitting, at most 60 characters. Example: 'Gather PC Components'.",
          },
          date: {
            type: "string",
            format: "date",
            description:
              "Calendar day as YYYY-MM-DD. MUST fall on or between the activity's start date and due date, inclusive, and MUST be a day the student said they are free.",
          },
          start_time: {
            type: "string",
            description:
              "Local start time in 24-hour HH:MM form, e.g. '10:00' or '14:30'. Must sit inside the student's stated available hours.",
          },
          end_time: {
            type: "string",
            description:
              "Local end time in 24-hour HH:MM form. Must be later than start_time on the same day, and the gap must not exceed the student's stated focus limit.",
          },
          detail: {
            type: "string",
            description:
              "One to three sentences describing exactly what to do in this sitting, concrete enough to start without re-reading the assignment.",
          },
        },
      },
    },
  },
} as const;

export interface PlanTask {
  title: string;
  date: string; // YYYY-MM-DD
  start_time: string; // HH:MM or ""
  end_time: string;
  detail: string;
}

export interface ActivityInput {
  name: string;
  course: string;
  start_date: string;
  due_date: string;
  details: {
    assignment: string;
    availability: string;
    focus: string;
    other: string;
  };
  today: string;
  think?: boolean;
  pdf?: { name: string; data: string } | null;
}

/** Coerce '2:30 PM', '14', '14:30' into 'HH:MM'. Returns "" if unusable. */
export function normalizeTime(raw: string | undefined): string {
  if (!raw) return "";
  const value = raw.trim();

  let m = /^(\d{1,2}):(\d{2})$/.exec(value);
  if (m) return clamp(+m[1], +m[2]);

  m = /^(\d{1,2}):(\d{2})\s*([ap])\.?m\.?$/i.exec(value);
  if (m) return clamp(to24(+m[1], m[3]), +m[2]);

  m = /^(\d{1,2})\s*([ap])\.?m\.?$/i.exec(value);
  if (m) return clamp(to24(+m[1], m[2]), 0);

  m = /^(\d{1,2})$/.exec(value);
  if (m) return clamp(+m[1], 0);

  return "";
}

function to24(hour: number, meridiem: string): number {
  const h = hour % 12;
  return meridiem.toLowerCase() === "p" ? h + 12 : h;
}

function clamp(hour: number, minute: number): string {
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return "";
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

/** Minutes parsed from free text like "4 hours" / "45 min". null = no limit stated. */
export function focusMinutes(text: string): number | null {
  const lower = (text || "").toLowerCase();
  const hours = /(\d+(?:\.\d+)?)\s*(?:h\b|hr|hour)/.exec(lower);
  if (hours) return Math.round(parseFloat(hours[1]) * 60);
  const mins = /(\d+)\s*(?:m\b|min)/.exec(lower);
  if (mins) return parseInt(mins[1], 10);
  return null;
}

function minutesBetween(start: string, end: string): number | null {
  if (!start || !end) return null;
  const [sh, sm] = start.split(":").map(Number);
  const [eh, em] = end.split(":").map(Number);
  return eh * 60 + em - (sh * 60 + sm);
}

function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${a}T00:00:00Z`) - Date.parse(`${b}T00:00:00Z`)) / 86400000);
}

/**
 * Find everything objectively wrong with a plan.
 *
 * Deliberately does NOT try to verify the free-text availability window
 * ("Aug 4, 6, 8, 9, 10-13, 15, 10AM-4PM") -- parsing that reliably is a
 * research project and a wrong parse would reject good plans.
 */
export function inspectPlan(tasks: PlanTask[], activity: ActivityInput): string[] {
  const warnings: string[] = [];
  const limit = focusMinutes(activity.details.focus);

  for (const task of tasks) {
    const afterDue = daysBetween(task.date, activity.due_date);
    if (afterDue > 0) {
      warnings.push(`'${task.title}' falls ${afterDue}d after the due date`);
    } else if (daysBetween(task.date, activity.start_date) < 0) {
      warnings.push(`'${task.title}' falls before the activity start date`);
    } else if (daysBetween(task.date, activity.today) < 0) {
      warnings.push(`'${task.title}' is scheduled in the past`);
    }

    const span = minutesBetween(task.start_time, task.end_time);
    if (span !== null && span <= 0) {
      warnings.push(`'${task.title}' ends at or before it starts`);
    } else if (span !== null && limit && span > limit) {
      warnings.push(`'${task.title}' runs ${span}min, past the ${limit}min focus limit`);
    }
  }

  return warnings;
}
