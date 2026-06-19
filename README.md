# AutoDevLoop

> Set one goal. Watch AI coding agents architect, build, test, review, and keep evolving a real project — version by version — until your target version count is reached.

**English** · [简体中文](README.zh-CN.md)

AutoDevLoop is a small, dependency-free Python tool that drives a CLI coding
agent (Claude Code by default; Codex / Gemini CLI also supported) through a
standardised, multi-stage development loop. You give it a goal and a number of
versions; it designs an architecture, plans each version, writes the code,
runs tests, reviews the result, and — once your goal is met — proposes and
value-gates **new** features to keep improving the product on its own.

Every version is a usable, snapshotted build. A glanceable `FEATURES.md` table
records what each version delivers and what changed since the last one.

---

## Highlights

- **Goal-driven, two-phase loop.** A **build** phase drives straight at your
  goal; once an independent check decides the goal is genuinely met, it flips to
  an **expand** phase that builds valuable adjacent features.
- **Value gate for new features.** In the expand phase, one agent scouts ideas
  and a *separate* agent scores each for value/effort — only accepted ideas
  enter the backlog and get built. No random feature bloat.
- **Standard flow, dynamic detail.** The pipeline (architecture → plan → develop
  → test → review → fix → scout → evaluate) is fixed and predictable, but the
  planner decides how many dev agents to spawn, what each does, and the prompts
  leave room for the model's judgement. Prompts are **editable template files**,
  not hard-coded strings.
- **Simple vs advanced modes.** `simple` runs a cheap core loop
  (plan → develop → test → review) to save tokens; `advanced` adds goal checks,
  a test-planning agent, docs, scouting, and the value gate. Individual steps
  are toggleable.
- **One working folder, many snapshots.** Agents only ever edit `current/`;
  each finished version is copied to `versions/vN/`. If git is available, every
  version is also committed and tagged, and the version where your goal is first
  met gets a special `goal-complete` tag.
- **Cost & token tracking.** Every provider call's cost and tokens are recorded
  and surfaced live.
- **Local web dashboard.** Start projects, watch live progress (current version,
  agent, step, cost, per-agent output), read the changelog/feature table, and
  edit configuration + prompts — all in the browser. No build step, no
  dependencies.
- **Resilient.** Threaded provider I/O (no stdin/stdout deadlocks), retry with
  exponential backoff on transient failures, atomic state writes, and automatic
  rollback of the working copy if a version errors out.

---

## Requirements

- **Python 3.10+**
- **A coding-agent CLI installed and authenticated locally**, one of:
  - [Claude Code](https://docs.claude.com/en/docs/claude-code) — `claude` (default)
  - Codex CLI — `codex`
  - Gemini CLI — `gemini`

AutoDevLoop **never asks for API keys**. You authenticate your CLI of choice
beforehand; switching providers just changes which command is invoked. (Using
a third-party API endpoint behind the `claude` CLI works fine — the tool just
calls `claude`.)

No Python runtime dependencies are required. `PyYAML` is optional (a built-in
fallback YAML parser ships with the tool).

---

## Install

```bash
# from the project root
pip install -e .
# now the `autodevloop` command is available
autodevloop --version
```

Or run without installing:

```bash
python -m autodevloop --help
# or the backward-compatible shim:
python autodev.py --help
```

---

## Quick start

### CLI

```bash
# Interactive (prompts for directory, goal, versions, mode):
autodevloop run

# Non-interactive:
autodevloop run --project-dir ./my-app \
  --goal "Build a WeChat-like app: real-time chat plus a moments feed" \
  --max-versions 8 --mode advanced

# Brainstorm the design first (interactive Q&A, then the loop runs):
autodevloop run --project-dir ./my-app --goal "a todo CLI" --brainstorm
```

**Brainstorm mode** (`--brainstorm`): before the autonomous loop starts, the AI
asks you **one question at a time** to refine purpose, scope, constraints and
success criteria — turning a rough idea into an agreed design. The transcript is
saved to `.autodev/brainstorm.json` (so it survives interruptions and is not
re-run on resume) and the final design to `docs/brainstorm-spec.md`; the refined
goal then feeds the run. Type `/done` to wrap up early or `/skip` to cancel.
It is also available as a checkbox + chat panel when creating a project in the
web dashboard. Skipped automatically in `--non-interactive` runs.

Watch / control a run:

```bash
autodevloop status --project-dir ./my-app
autodevloop stop   --project-dir ./my-app     # graceful stop after the current step
```

### Web dashboard

```bash
autodevloop web            # http://127.0.0.1:8787
autodevloop web --port 9000
```

The UI is available in **English / 简体中文 / 日本語** (compact 🌐 switcher,
top-right). A built-in **Help** guide and hover tooltips (the `?` icons) explain
every setting, agent, and button, so first-time users aren't left guessing.

From the dashboard you can:

1. **Create a project** — directory, goal, version count, mode, provider,
   architecture hint. Creating only *creates* it; you then review/edit settings
   and press **Run** when ready (it does not auto-start).
2. **Watch live** — status, phase, current version, a **per-agent live timer**
   for every agent running right now (multiple at once when agents run in
   parallel), agent-call count, token usage, total run time, a scrollable
   activity log with a **divider between versions**, and each agent's full
   output (persistent viewer).
3. **Edit settings** — pipeline mode and step toggles, max versions, review and
   value thresholds, retries, test command, provider command/model, and **every
   prompt template**. Required agents (plan, develop, test, review, fix) are
   shown but locked on; only optional steps can be toggled. Prompt edits are
   **format-checked** — rewrite the wording in any language, but the
   `{{placeholders}}` and JSON field names the engine depends on can't be
   removed. Settings are **locked while a run is active** and take effect on the
   next run.
4. **Stop two ways** — *graceful* (finish the current version, then stop) or
   *discard* (kill immediately, throw away the unfinished version, and roll the
   working copy back to the last completed version). Each shows a confirmation
   explaining exactly what happened.
5. **Read the docs** — the `FEATURES.md` overview table and `CHANGELOG.md`.

> Cost in money is intentionally not shown (third-party API pricing behind a CLI
> is unreliable); the dashboard reports **agent-call count and tokens** instead.

---

## How the loop works

```
            ┌─────────────────────────── once, at the start ──────────────────────────┐
            │  AgentARCH → picks a mainstream stack, layout, run & test strategy        │
            └──────────────────────────────────────────────────────────────────────────┘
 per version:
   AgentPLAN ── decides this version's goal + how many dev agents and what they own
       │
   AgentDEV_* ─ one or more (parallel) agents implement in isolated workspaces,
       │        then merge back into current/ (only changed files; first-writer-wins
       │        on conflict, with a warning)
   AgentDOC ── (advanced) keeps README / design docs accurate
       │
   AgentTEST ─ runs tests: built-in detection in simple mode, or an agent picks
       │        the test commands in advanced mode
   AgentREVIEW  scores quality, flags blockers, judges goal completeness, and
       │        writes the human-readable "what's new" summary
       │
   (fix loop) ─ if tests fail / blocking / below threshold, AgentFIX repairs and re-tests
       │
   AgentGOALCHECK (advanced) independently confirms whether the goal is met
       │
   ── if goal met for the first time → switch to EXPAND phase, tag goal-complete ──
       │
   AgentSCOUT + AgentEVALUATE (expand phase) propose & value-gate new features
       │        into a persistent backlog the planner draws from next time
       ▼
   snapshot → versions/vN/, git commit + tag vN, update CHANGELOG.md & FEATURES.md
```

The loop never stops early for being "good enough" — it runs until your
`max_versions` (or you stop it). Reaching the goal switches *what* it works on,
not *whether* it keeps going.

---

## Output layout

Each project directory gets:

| Path | What |
|---|---|
| `current/` | The single working copy agents edit (git repo if enabled) |
| `versions/vN/` | A full snapshot of every completed version |
| `FEATURES.md` | At-a-glance table: each version's features + what changed |
| `CHANGELOG.md` | Per-version changelog with summaries and test status |
| `.autodev/state.json` | Full run state |
| `.autodev/progress.json` | Live progress + event feed (used by the web UI) |
| `.autodev/backlog.json` | Scouted features and their accept/reject verdicts |
| `.autodev/architecture.md` | The initial architecture report |
| `.autodev/prompts/templates/` | Editable prompt templates |
| `.autodev/plans/`, `reviews/`, `tests/`, `logs/` | Per-stage artifacts |
| `.autodev/final_report.md` | Summary written at the end of a run |

---

## Configuration

Settings live in `.autodevloop.yml` in the project directory (the web settings
page and CLI flags write to it). Everything has a sane default; a full file
looks like:

```yaml
project:
  name: My App
  max_versions: 8
  arch_hint: "React + FastAPI + SQLite"   # optional hint for AgentARCH

provider:
  name: claude          # claude | codex | gemini
  command: ""           # blank = use the profile's default command (e.g. "claude")
  model: ""             # optional model alias/name
  extra_args: []        # extra CLI args appended to every call

pipeline:
  mode: advanced        # simple | advanced
  steps:                # override individual steps on top of the mode defaults
    goal_check: true
    test_agent: true
    doc: true
    scout: true
    evaluate: true
    features_doc: true

agents:
  timeout: 1800         # seconds per provider call
  allow_parallel: true
  max_parallel: 3
  retries: 3            # retries on transient provider failures
  backoff_seconds: 5

review:
  threshold: 80         # review score below this triggers a fix pass

value:
  threshold: 65         # feature value below this is rejected by the gate

fix:
  retries: 2

tests:
  timeout: 120
  command: ""           # blank = auto-detected / agent-chosen

vcs:
  git: true             # commit + tag each version inside current/
```

Useful CLI flags: `--mode`, `--provider`, `--provider-command`, `--model`,
`--max-versions`, `--review-threshold`, `--fix-retries`, `--max-parallel-agents`,
`--no-parallel`, `--no-git`, `--test-command`, `--reset`, `--non-interactive`.

---

## ⚠️ Security notice — please read

AutoDevLoop is an **autonomous code generator that runs code on your machine**.
Treat it like any tool that executes untrusted code:

- **It writes and executes code.** Agents run with file edit permissions, and
  `AgentTEST` runs shell test/build commands in your project directory. Generated
  code is not reviewed by a human before it runs.
- **It runs unattended and can spend money.** The loop keeps calling your
  provider CLI until it reaches the version count or you stop it. Watch the live
  cost readout, set a sensible `--max-versions`, and keep an eye on your provider
  billing.
- **Run it in an isolated environment.** Prefer a dedicated directory, a
  container, or a VM. Don't point it at a directory containing secrets or
  important unrelated files.
- **The web dashboard is unauthenticated and binds to localhost.** It can start
  runs and execute commands. Do **not** expose the port to a network you don't
  fully trust. There is no auth layer.
- **No API keys are handled by this tool** — your provider CLI manages its own
  credentials. AutoDevLoop only invokes the command you configured.

By running AutoDevLoop you accept that you are responsible for the code it
generates and the commands it executes.

---

## Troubleshooting

- **"Provider command not found"** — install the CLI and ensure it's on `PATH`,
  or set `provider.command` to the full path / wrapper command.
- **Git commits don't appear** — git is optional; the tool falls back to folder
  snapshots. Corporate git hooks that block commits are tolerated silently.
- **Garbled characters on a legacy Windows console** — output is forced to UTF-8;
  if your terminal still struggles, run inside Windows Terminal or set
  `PYTHONIOENCODING=utf-8`.

---

## License

[MIT](LICENSE). Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
