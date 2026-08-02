/**
 * boost — edge half.
 *
 * Serves the installer and the shell CLI as plain text, and exposes one API
 * endpoint that turns an assignment into daily tasks. The Anthropic key lives
 * here as a Worker secret, so end users never need one of their own — which
 * also means every request spends the owner's tokens, hence the rate limit.
 *
 * Routes are scoped to /boost and /boost/* only; the rest of kojesko.dev is
 * untouched.
 */

import Anthropic from "@anthropic-ai/sdk";

import boostScript from "../scripts/boost.sh";
import installScript from "../scripts/install.sh";
import {
  ActivityInput,
  MAX_TOKENS,
  MODEL,
  PLAN_SCHEMA,
  PlanTask,
  SYSTEM,
  THINKING_BUDGET,
  inspectPlan,
  normalizeTime,
} from "./plan";

/** Hard ceiling on the request body. Base64 PDFs dominate; 12 MB is generous. */
const MAX_BODY_BYTES = 12 * 1024 * 1024;
/** ~6 MB of PDF once base64 inflation is accounted for. */
const MAX_PDF_B64 = 8_000_000;
/** Per-field cap so a pathological "details" blob cannot burn the token budget. */
const MAX_FIELD = 6000;

const SH = "text/x-shellscript; charset=utf-8";
const JSON_TYPE = "application/json; charset=utf-8";
const TSV_TYPE = "text/tab-separated-values; charset=utf-8";

function json(body: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": JSON_TYPE, "cache-control": "no-store", ...extra },
  });
}

/**
 * The shell CLI sends `Accept: text/tab-separated-values` so it can read plans
 * with awk. Shipping a second representation here is far safer than asking a
 * POSIX shell to parse JSON, and it keeps `curl | sh` free of a jq dependency.
 */
function wantsTsv(request: Request): boolean {
  return (request.headers.get("accept") ?? "").includes("text/tab-separated-values");
}

/**
 * Tabs and newlines are the record separators, so they can never appear inside
 * a field. Collapsing them is lossy by design -- the alternative is an escaping
 * scheme that every awk consumer would then have to reverse.
 */
function tsvField(value: string): string {
  return value.replace(/[\t\r\n]+/g, " ").trim();
}

function tsv(rows: string[][], status = 200): Response {
  const body = rows.map((row) => row.map(tsvField).join("\t")).join("\n");
  return new Response(body + "\n", {
    status,
    headers: { "content-type": TSV_TYPE, "cache-control": "no-store" },
  });
}

function script(body: string): Response {
  return new Response(body, {
    headers: {
      "content-type": SH,
      // Short cache: installs should pick up fixes quickly, but a burst of
      // curls during a workshop shouldn't all hit the origin.
      "cache-control": "public, max-age=300",
      "x-content-type-options": "nosniff",
    },
  });
}

/**
 * Errors negotiate too. If only success responses were TSV, the CLI would meet
 * a JSON body exactly when something went wrong and it most needs the message.
 */
function fail(message: string, status: number, hint?: string, request?: Request): Response {
  if (request && wantsTsv(request)) {
    return tsv([["E", message, hint ?? ""]], status);
  }
  return json({ error: message, ...(hint ? { hint } : {}) }, status);
}

function clip(value: unknown): string {
  return typeof value === "string" ? value.slice(0, MAX_FIELD) : "";
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Validate and normalize the client payload. Throws a user-facing message. */
function parseActivity(raw: any): ActivityInput {
  if (!raw || typeof raw !== "object") throw new Error("Body must be a JSON object.");

  const name = clip(raw.name).trim();
  const details = raw.details ?? {};
  const assignment = clip(details.assignment).trim();

  if (!name) throw new Error("`name` is required.");
  if (!assignment) throw new Error("`details.assignment` is required.");

  for (const field of ["start_date", "due_date", "today"]) {
    if (!ISO_DATE.test(String(raw[field] ?? ""))) {
      throw new Error(`\`${field}\` must be YYYY-MM-DD.`);
    }
  }
  if (raw.due_date < raw.start_date) {
    throw new Error("`due_date` is before `start_date`.");
  }

  let pdf: ActivityInput["pdf"] = null;
  if (raw.pdf && typeof raw.pdf.data === "string") {
    if (raw.pdf.data.length > MAX_PDF_B64) {
      throw new Error(
        `Attachment is too large (${Math.round(raw.pdf.data.length / 1.37 / 1e6)} MB). Limit is about 6 MB.`,
      );
    }
    pdf = { name: clip(raw.pdf.name) || "attachment.pdf", data: raw.pdf.data };
  }

  return {
    name,
    course: clip(raw.course).trim(),
    start_date: raw.start_date,
    due_date: raw.due_date,
    today: raw.today,
    think: Boolean(raw.think),
    details: {
      assignment,
      availability: clip(details.availability).trim(),
      focus: clip(details.focus).trim(),
      other: clip(details.other).trim(),
    },
    pdf,
  };
}

function buildContent(activity: ActivityInput): Anthropic.ContentBlockParam[] {
  const content: Anthropic.ContentBlockParam[] = [];

  // Documents go before the instruction text -- the order the API expects.
  if (activity.pdf) {
    content.push({
      type: "document",
      source: { type: "base64", media_type: "application/pdf", data: activity.pdf.data },
      title: activity.pdf.name,
    });
  }

  const span =
    Math.round(
      (Date.parse(`${activity.due_date}T00:00:00Z`) -
        Date.parse(`${activity.start_date}T00:00:00Z`)) /
        86400000,
    ) + 1;

  let text =
    `Today is ${activity.today}.\n\n` +
    `Activity name: ${activity.name}\n` +
    `Class: ${activity.course || "(not given)"}\n` +
    `Activity start date: ${activity.start_date}\n` +
    `Activity due date: ${activity.due_date}\n` +
    `Days available: ${span}\n\n` +
    `My assignment (what I need to do, in detail):\n${activity.details.assignment}\n\n` +
    `When I can study (days/times):\n${activity.details.availability || "(not given)"}\n\n` +
    `How long I can focus at a time:\n${activity.details.focus || "(not given)"}\n\n` +
    `Other details that might help:\n${activity.details.other || "(none)"}\n`;

  if (activity.pdf) {
    text +=
      "\nThe attached PDF is the assignment brief. Use its actual requirements " +
      "and deadlines; where it conflicts with the notes above, the PDF wins.\n";
  }
  text += "\nBreak this into daily tasks.";

  content.push({ type: "text", text });
  return content;
}

async function handlePlan(request: Request, env: Env): Promise<Response> {
  // Every error out of this handler has to honour the client's Accept header,
  // so bind the request once rather than threading it through 14 call sites.
  const bad = (message: string, status: number, hint?: string) =>
    fail(message, status, hint, request);

  if (!env.ANTHROPIC_API_KEY) {
    return bad(
      "This deployment has no Anthropic key configured.",
      503,
      "The operator needs to run: wrangler secret put ANTHROPIC_API_KEY",
    );
  }

  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_BODY_BYTES) {
    return bad("Request body too large.", 413);
  }

  // Rate limit per client IP. Every call spends the operator's tokens, so this
  // is a cost control, not just an abuse control.
  const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
  const { success } = await env.PLAN_LIMIT.limit({ key: ip });
  if (!success) {
    return bad("Too many plans in a short window. Wait a minute and retry.", 429, undefined);
  }

  let activity: ActivityInput;
  try {
    activity = parseActivity(await request.json());
  } catch (err) {
    return bad(err instanceof Error ? err.message : "Malformed request body.", 400);
  }

  const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });

  const params: Anthropic.MessageCreateParamsNonStreaming = {
    model: MODEL,
    max_tokens: MAX_TOKENS,
    system: SYSTEM,
    messages: [{ role: "user", content: buildContent(activity) }],
    // Constrains generation itself, so the model cannot emit a shape that fails
    // to parse -- no markdown fences, no trailing prose.
    output_config: { format: { type: "json_schema", schema: PLAN_SCHEMA } },
  } as Anthropic.MessageCreateParamsNonStreaming;

  if (activity.think) {
    // Haiku 4.5 predates adaptive thinking: it takes an explicit token budget,
    // which must be >= 1024 and stay below max_tokens. Passing `effort` here --
    // the newer control used by Opus/Sonnet 5 -- would be a 400.
    params.thinking = { type: "enabled", budget_tokens: THINKING_BUDGET };
  }

  let message: Anthropic.Message;
  try {
    message = await client.messages.create(params);
  } catch (err) {
    if (err instanceof Anthropic.RateLimitError) {
      return bad("Upstream is rate limited. Try again shortly.", 429);
    }
    if (err instanceof Anthropic.AuthenticationError) {
      return bad("The configured Anthropic key was rejected.", 502);
    }
    // APIConnectionError extends APIError in the TS SDK (unlike Python, where
    // they are siblings), so it has to be tested first or the generic branch
    // swallows every network failure. There is no APIStatusError here -- that
    // class is Python-only, and referencing it made `instanceof` throw.
    if (err instanceof Anthropic.APIConnectionError) {
      return bad("Could not reach the Anthropic API.", 504);
    }
    if (err instanceof Anthropic.APIError) {
      return bad(`Upstream error ${err.status ?? "unknown"}: ${err.message}`, 502);
    }
    console.error("plan_failed", { message: String(err) });
    return bad("Planning failed.", 500);
  }

  if (message.stop_reason === "refusal") {
    return bad("Claude declined to plan this assignment. Try rephrasing the details.", 422);
  }
  if (message.stop_reason === "max_tokens") {
    return bad("The plan was cut off before it finished. Shorten the details and retry.", 502);
  }

  // With think=true the response leads with thinking blocks, so scan for the
  // first text block rather than assuming content[0].
  const textBlock = message.content.find((b): b is Anthropic.TextBlock => b.type === "text");
  if (!textBlock) return bad("Model returned no text content.", 502);

  let payload: { summary?: string; tasks?: PlanTask[] };
  try {
    payload = JSON.parse(textBlock.text);
  } catch {
    return bad("Model returned unparseable JSON.", 502);
  }

  const tasks: PlanTask[] = (payload.tasks ?? [])
    .filter((t) => t && ISO_DATE.test(String(t.date)))
    .map((t) => ({
      title: String(t.title ?? "Untitled task").trim().slice(0, 120),
      date: t.date,
      start_time: normalizeTime(t.start_time),
      end_time: normalizeTime(t.end_time),
      detail: String(t.detail ?? "").trim().slice(0, 800),
    }))
    .sort((a, b) => a.date.localeCompare(b.date) || a.start_time.localeCompare(b.start_time));

  if (tasks.length === 0) return bad("The model returned an empty plan.", 502);

  const summary = String(payload.summary ?? "").trim();
  const warnings = inspectPlan(tasks, activity);

  if (wantsTsv(request)) {
    // Record-per-line, tag in column 1. Field order is positional and stable,
    // so the shell reads it with a single awk pass and no lookahead.
    //   S  summary
    //   W  warning
    //   T  date  start  end  title  detail
    //   U  input_tokens  output_tokens
    return tsv([
      ["S", summary],
      ...warnings.map((w) => ["W", w]),
      ...tasks.map((t) => ["T", t.date, t.start_time, t.end_time, t.title, t.detail]),
      ["U", String(message.usage.input_tokens), String(message.usage.output_tokens)],
    ]);
  }

  return json({
    summary,
    tasks,
    warnings,
    usage: {
      input_tokens: message.usage.input_tokens,
      output_tokens: message.usage.output_tokens,
    },
  });
}

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/boost";

    try {
      // `curl -fsSL https://kojesko.dev/boost | sh`
      if (path === "/boost" && request.method === "GET") {
        return script(installScript);
      }

      // The CLI itself, fetched by the installer.
      if (path === "/boost/bin" && request.method === "GET") {
        return script(boostScript);
      }

      if (path === "/boost/api/health" && request.method === "GET") {
        return json({
          ok: true,
          model: MODEL,
          key_configured: Boolean(env.ANTHROPIC_API_KEY),
        });
      }

      if (path === "/boost/api/plan") {
        if (request.method !== "POST") {
          return fail("Use POST.", 405, "POST /boost/api/plan");
        }
        return await handlePlan(request, env);
      }

      return fail(`No such endpoint: ${path}`, 404, "Try GET /boost");
    } catch (err) {
      // Explicit catch rather than passThroughOnException, so failures surface
      // as structured JSON instead of falling through to the portfolio SPA.
      console.error("unhandled", { path, message: String(err) });
      return fail("Internal error.", 500);
    }
  },
} satisfies ExportedHandler<Env>;
