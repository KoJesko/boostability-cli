# boost

Turn an assignment into achievable daily tasks, from the terminal.

You describe what you have to do, when you're free, and how long you can focus.
It returns a small number of concrete, scheduled sittings you can actually sit
down and start — not a wall of advice.

```
$ boost add

  Build PC for Schmidt   SSUR Summer Undergrad Research    Due Monday, August 17th
  ○ 0/6   Aug 2nd → Aug 17th

  I've planned your PC build and video documentation across six focused
  sessions, front-loading component gathering before recording and shipping.

  Tuesday, August 4th
  [ ] 1.1  Gather PC components and finalize build list    10:00 – 14:00
        Research and purchase all necessary components: CPU, motherboard,
        RAM, GPU, power supply, case, storage, cooling. Verify compatibility.

  Thursday, August 6th
  [ ] 1.2  Assemble PC and install motherboard/CPU         10:00 – 14:00
        ...
```

---

## Install

```sh
curl -fsSL https://kojesko.dev/boost | sh
```

That's the whole thing. **You do not need an Anthropic API key** — the key lives
on the server, so the CLI is just `curl` and `awk`.

The installer drops a single POSIX shell script at `~/.local/bin/boost`. To put
it somewhere else, set `BOOST_BIN_DIR`. To point at your own deployment, set
`BOOST_API`.

Requirements: `curl`, `awk`, `sed`, `sort`, `base64` — nothing outside a POSIX
base system. Notably **no `jq`** and no Python, Node, or package manager.

---

## Use

```sh
boost                    # dashboard: everything tracked, plus today
boost add                # add an assignment and break it down
boost list               # all tracked activities
boost show 1             # one activity's full plan
boost today -d 7         # what's scheduled over the next week
boost done 1.2           # tick off activity 1, task 2
boost undo 1.2
boost replan 1 --force   # regenerate the plan
boost rm 1 --force
boost export 1 --format ics -o plan.ics   # or --format md
boost where              # store path, API, model in use
boost update             # reinstall the latest version
```

`boost add` is interactive by default. It's also fully scriptable:

```sh
boost add \
  --name  "Build PC for Schmidt" \
  --class "SSUR Summer Undergrad Research" \
  --start 2026-08-02 --due 2026-08-17 \
  --details      "Build a PC to demo how easy installing apps on Linux is..." \
  --availability "Aug 4, 6, 8, 9, 10-13, 15, 10AM-4PM" \
  --focus        "4 hours"
```

Dates accept ISO (`2026-08-17`), US slash (`8/17/2026`), `today`, `tomorrow`,
or `+N` days.

Attach the actual assignment brief and it will be read directly:

```sh
boost add --pdf ~/Downloads/assignment.pdf
```

`boost export --format ics` writes a standard iCalendar file, so a plan can be
dropped straight into Google Calendar, Apple Calendar, or Outlook. Events use
floating local time, so they land at the same wall-clock hour wherever opened.

---

## How it works

Two halves, plus a reference implementation.

```
   your terminal                    Cloudflare edge              Anthropic
  ┌──────────────┐                ┌─────────────────┐          ┌──────────┐
  │  boost (sh)  │ ──── HTTPS ──▶ │  Worker         │ ──────▶  │ Haiku4.5 │
  │              │  Accept: TSV   │  holds the key  │          └──────────┘
  │  store.tsv   │ ◀───────────── │  rate limits    │
  └──────────────┘                └─────────────────┘
```

### The Worker (`worker/`)

Serves three things off the `/boost` path and nothing else, so it can sit on a
domain that already hosts something at `/`:

| Route | Purpose |
| --- | --- |
| `GET /boost` | the installer script |
| `GET /boost/bin` | the CLI itself |
| `POST /boost/api/plan` | turn an assignment into tasks |
| `GET /boost/api/health` | liveness + which model is configured |

The two shell scripts live as **real files** in `worker/scripts/` and are inlined
into the bundle as strings by Wrangler's `Text` module rule — so they're
editable, diffable, and syntax-checkable, rather than heredocs buried in
TypeScript.

Because the Worker holds the API key, every request spends *the operator's*
tokens. That makes the rate limit a cost control, not just an abuse control: 8
plans per minute per IP, enforced by a Cloudflare rate-limit binding.

### Why the model is what it is

`claude-haiku-4-5` — **the cheapest model that can do this job**, at $1/$5 per
million input/output tokens. Selecting it isn't just about price:

- The task needs **structured outputs** (to guarantee parseable JSON) and **PDF
  input** (for assignment briefs). Haiku 4.5 is the cheapest model supporting
  both.
- The genuinely cheaper legacy Haikus are dead ends: Haiku 3.5 ($0.80/$4) is
  retired, Haiku 3 ($0.25/$1.25) is deprecated, and **neither supports
  structured outputs** at all.
- It's a 200K-context model, which caps PDFs at 100 pages rather than 600.
- It predates adaptive thinking, so `--think` sends
  `{"type": "enabled", "budget_tokens": N}`. Passing `effort` — the newer
  control used by Opus/Sonnet 5 — would be a 400 on this model.

A typical plan costs well under a cent.

### Two layers of constraint

A JSON Schema constrains the **shape** of what the model returns; ordinary code
constrains its **meaning**.

Structured outputs guarantee valid JSON — no markdown fences, no trailing prose,
no parse step that can fail. But no schema can stop a model from placing a task
three days after the due date. Date arithmetic is where language models are
weakest, so everything checkable in code is checked in code
(`worker/src/plan.ts` → `inspectPlan`):

- a task after the due date, before the start date, or in the past
- a task that ends at or before it starts
- a block longer than the stated focus limit

Those surface as warnings under the plan rather than silently corrupting it.
The checker deliberately does **not** try to parse free-text availability like
`"Aug 4, 6, 8, 9, 10-13, 15, 10AM-4PM"` — doing that reliably is a research
project, and a wrong parse would reject good plans.

### Why the API speaks TSV

`POST /boost/api/plan` returns JSON normally, but returns tab-separated values
when asked with `Accept: text/tab-separated-values`.

This exists so a `curl | sh` CLI doesn't need `jq`. Parsing JSON in POSIX shell
is genuinely unsafe — quotes, escapes, and nesting all have to be handled by
hand — and requiring `jq` would mean many first-runs simply fail. Offering the
same resource in a second representation is ordinary HTTP content negotiation,
and it turns the entire client into one `awk` pass. Errors negotiate too;
otherwise the CLI would meet a JSON body exactly when it most needs to read the
message.

```
S    <summary>
W    <warning>
T    <date>  <start>  <end>  <title>  <detail>
U    <input_tokens>   <output_tokens>
E    <error message>  <hint>
```

### The local store

`~/.local/share/boostability/store.tsv`, line-oriented with a tag in column 1:

```
A  <id>  <name>  <course>  <start>  <due>  <summary>
D  <id>  <assignment>  <availability>  <focus>  <other>
T  <id>  <done>  <date>  <start>  <end>  <title>  <detail>
```

Every read is a single `awk` pass; every write rewrites the stream to a temp
file and renames, so an interrupted write can't truncate the store. Tabs and
newlines are stripped from user input on the way in, so no field can break the
layout. A task's position among its activity's `T` lines is its index — that's
what the `3.2` in `boost done 3.2` refers to.

Calendar maths (weekday names, "3d overdue") is done with the Julian Day Number
formula *inside awk*, because `date -d` (GNU) and `date -v` (BSD) are mutually
incompatible and calling out once per row would be both slow and unportable.

### The Python reference (`boost/`)

`boost/` is a fuller local-first implementation of the same idea: same prompt,
same schema, same validation rules, plus an interactive `rich` TUI with a
navigable checklist. It talks to Anthropic **directly with your own key**
(`ANTHROPIC_API_KEY` or `~/.config/boostability/config.toml`).

```sh
pip install -e .
boost add
```

It's the reference the edge half was ported from. Use it if you want the
interactive checklist or would rather spend your own tokens; use the shell CLI
if you want a zero-dependency install and no key of your own.

---

## Deploy your own

```sh
cd worker
npm install

# 1. point it at your domain (or delete `routes` to use a workers.dev subdomain)
$EDITOR wrangler.jsonc

# 2. give it a key — this is the only place the secret should ever live
npx wrangler secret put ANTHROPIC_API_KEY

# 3. ship it
npx wrangler deploy
```

Local development:

```sh
cp .dev.vars.example .dev.vars     # then put a real key in it; gitignored
npx wrangler dev

# in another shell
BOOST_API=http://127.0.0.1:8787/boost sh scripts/boost.sh add
```

Regenerate binding types after changing `wrangler.jsonc`:

```sh
npx wrangler types
```

> **On keys:** the API key belongs in `wrangler secret` (deployed) or
> `.dev.vars` (local) — never in a source file. A key in `src/env.d.ts` doesn't
> even work: a `.d.ts` is erased at compile time, so it declares a *type* and
> never supplies a *value*. It would ship the secret to your repo and still
> leave the Worker returning "no Anthropic key configured".

---

## Built with AI

This project uses AI in two distinct places, and it's worth separating them.

### AI in the product

The planning step is a single Claude call. The engineering around it is the
interesting part, and the guiding principle is **narrow the model's job until it
can't fail in an expensive way**:

- **Constrain the output format at generation time.** Structured outputs mean
  the model *cannot* emit an unparseable shape. There is no regex, no fence
  stripping, no retry-on-bad-JSON.
- **Verify meaning in ordinary code.** The schema can't express "on or before
  the due date," so `inspectPlan` checks it afterwards. Anything a computer can
  check deterministically is not left to the model.
- **Normalize instead of rejecting.** Structured outputs don't support `pattern`,
  so times can't be schema-constrained to `HH:MM`. The model occasionally
  returns `2:30 PM` or `14`. `normalizeTime` coerces those rather than failing
  the whole plan over a formatting slip.
- **Prompt for the shape of a good answer, not just the task.** The system
  prompt encodes what makes a task actionable — one sitting, a clear finish
  line, the student's own vocabulary, three to eight tasks, front-loaded.

### AI in the build

The codebase was written with [Claude Code](https://claude.com/claude-code),
working from a UI mockup of the original web version toward a terminal
equivalent. Roughly how it went, and what actually mattered:

- **The Python implementation came first** as a reference, then was ported to
  TypeScript at the edge. Having a known-good version to port *from* made the
  second implementation much faster to verify — the prompt, schema, and
  validation rules could be compared side by side.
- **The model's own API is the thing it's least reliable about.** Details like
  "Haiku 4.5 takes `budget_tokens`, not `effort`", or which model IDs are
  current, come from documentation at build time, not from recall. Model
  lineups change faster than training data.
- **Types and tests caught the real bugs.** A typecheck surfaced that the
  TypeScript port had carried over `Anthropic.APIStatusError` from the Python
  SDK — a class that doesn't exist in the JS SDK. Since `x instanceof undefined`
  throws, the error handler would itself have crashed on *any* upstream API
  error, turning a clean 502 into an unhandled 500. That's a bug a human
  reviewer skims past and a compiler finds instantly.
- **End-to-end runs caught the rest.** `export --format md` returned nothing
  because an awk variable was passed as a file argument rather than with `-v`,
  so it wasn't set during `BEGIN`. Nothing but running it would have shown that.

The honest summary: AI wrote most of the lines, and the leverage came from
constraining it — schemas, types, a reference implementation, and actually
executing the thing — rather than from trusting the output.

---

## Open design decision

`reconcile_plan()` in `boost/validate.py` is a deliberate `TODO`. It decides
what to *do* when `inspect_plan` finds a problem, and the shipped behaviour is
the permissive baseline: keep every task, show every warning, let the student
fix it by hand.

That's defensible, but it isn't the only answer. **Clamp** out-of-range tasks
back into the window (nothing is lost, but you can silently stack four tasks
onto one day). **Drop** them (the plan is always clean, but dropping a late task
usually means dropping the final deliverable — the single most important item).
**Retry** by handing the issue list back to the model (highest quality, costs a
round trip, can loop if the constraints are genuinely unsatisfiable). A hybrid
is probably right: retry for tasks after the due date, warn for focus-limit
overruns. `Issue.code` exists so you can branch per kind.

---

## Layout

```
boost/                  Python reference implementation (own key, rich TUI)
worker/
  src/index.ts          routing, validation, the Anthropic call, TSV negotiation
  src/plan.ts           prompt, JSON schema, deterministic plan checks
  src/env.d.ts          merges ANTHROPIC_API_KEY into the generated Env type
  scripts/install.sh    what `curl | sh` executes
  scripts/boost.sh      the CLI itself
  wrangler.jsonc        routes, rate limit, Text rule for *.sh
```

## Licence

NOT FOR PUBLIC USE.
