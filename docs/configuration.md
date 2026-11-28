# Configuration reference

Use the supervisor's reviewed JSON policy for `roadrunner run`. The interactive Claude hook workflow has its own [roadmap and settings](#claude-hook-configuration).

## Supervised plans

`roadrunner sync --roadmap PATH` accepts a YAML document or a Markdown document with one fenced YAML block containing a `tasks` list. Give each task a stable ID such as `TASK-001`, a title, a goal, measurable `acceptance_criteria`, and repository-relative `files_expected`. Use `depends_on` for task IDs that must resolve first and `priority` to order eligible tasks. Lower priorities run first, with task IDs breaking ties. A missing priority defaults to 100.

`sync` reports the frozen plan's absolute path, source revisions, and dispositions. Use that path as `run --plan`. Synchronization resets imported status and strips source validation commands. The supervisor takes all executable gates from its separately reviewed run policy.

### GitHub intake rules

`sync --github` reads through the authenticated `gh` CLI. Provide an operator-reviewed JSON map keyed by the source IDs reported by sync:

```json
{
  "github:OWNER/REPO#123": {
    "goal": "Normalize whitespace in parser output.",
    "acceptance_criteria": ["The committed regression test passes."],
    "files_expected": ["src/parser.py"],
    "depends_on": [],
    "priority": 10
  }
}
```

After an issue changes or reopens, review the updated scope. Set `approved_revision` to the revision shown for that item, then sync again. An externally closed issue stays externally closed.

## Supervised run policy

See the [example policy](shipping-run-policy.json) and the [minimal walkthrough](WORKFLOW.md#first-supervised-run). The table lists defaults applied by the controller.

| Field | Default | Purpose |
| --- | --- | --- |
| `adapter` | `claude` | Live Claude worker. The `fixture` adapter is for disposable testing. |
| `sandbox` | `macos` | Uses macOS `sandbox-exec`. Fixture isolation is available only with the fixture adapter. |
| `delivery` | `local` | Integrates into a local Git branch. |
| `validation_commands` | Required | Nonempty list of operator-reviewed shell checks run with network access disabled in macOS isolation. |
| `protected_paths` | `[]` | Repository-relative files or directories the worker cannot change. Include gate scripts and their supporting fixtures. |
| `max_attempts` | `3` | Maximum attempts per task. Must be a positive integer. |
| `attempt_seconds` | `300` | Maximum time per worker attempt. |
| `validation_seconds` | `120` | Validation deadline in seconds. |
| `run_seconds` | `1800` | Overall run deadline in seconds. |
| `max_budget_usd` | `3.0` | Total reservation limit for the run. |
| `attempt_budget_usd` | `1.0` | Native Claude cap reserved before each attempt. |

Time and budget values must be positive and finite. Dependencies needed by validation must be available before the run. Review the policy and run `roadrunner doctor --policy PATH` with the same environment you intend to use for `run`.

## Delivery policy

Pass a separate JSON file to `roadrunner deliver --run RUN_UUID --policy PATH`:

```json
{
  "repository": "OWNER/REPO",
  "base": "main",
  "required_checks": ["tests"],
  "allow_push": false,
  "allow_pr": false,
  "allow_merge": false,
  "allow_close": false
}
```

Use exact check names from the target repository. The default invocation prepares an artifact for review. Execution additionally requires `--execute`, authenticated Git and `gh`, and the applicable capabilities set to the JSON boolean `true`. All selected items must have verified local integration. Live GitHub delivery has yet to be qualified.

## Claude hook configuration

The rest of this page covers the interactive hook loop. It uses `tasks/tasks.yaml`, `.roadmap_state.json`, and `.context_snapshot.json`. Its environment variables do not configure the supervisor.

### Hook tunables

All tunables live at the top of `src/roadrunner/cli.py` so operators have a single
place to adjust retention and safety knobs. Change them in source — there is
no runtime override for these (use the env var path documented below for the
iteration cap).

| Constant                     | Default         | Purpose                                                                                                               |
| ---------------------------- | --------------- | --------------------------------------------------------------------------------------------------------------------- |
| `DEFAULT_VALIDATION_TIMEOUT` | `300` seconds   | Per-validation-command timeout. Overridable per task via the `validation_timeout` field in `tasks.yaml`.              |
| `MAX_TASK_ATTEMPTS`          | `5`             | Auto-block threshold. If the Stop hook observes the same task `in_progress` across this many resume cycles without a `complete`, the task is flipped to `blocked` and the loop moves on. |
| `TASKS_BACKUP_KEEP`          | `5`             | Rolling `tasks.yaml.bak.N` copies retained on every `save_tasks` write. Oldest is evicted atomically via `Path.replace`. |
| `LOG_ROTATE_BYTES`           | `10 * 1024²`    | Trigger threshold (10 MiB) for rotating `logs/trace.jsonl` and `logs/CHANGELOG.md`. Rotation runs at every task boundary. |
| `LOG_RETAIN_DAYS`            | `7`             | Rotated/compressed logs older than this are deleted at the next rotation pass.                                        |
| `STATE_SCHEMA_VERSION`       | `3`             | On-disk schema for `.roadmap_state.json`. Bump when the format changes incompatibly. See the migration contract below. |
| `SNAPSHOT_SCHEMA_VERSION`    | `1`             | On-disk schema for `.context_snapshot.json`. Bump when the PreCompact snapshot format changes incompatibly.           |

### Per-task validation timeout

`DEFAULT_VALIDATION_TIMEOUT` is the fallback. Individual tasks override it by
setting `validation_timeout: <seconds>` on the task:

```yaml
- id: ROAD-099
  title: "Long-running integration test"
  validation_timeout: 900     # 15 minutes for this task only
  validation_commands:
    - ./scripts/slow-e2e.sh
```

The override must be a positive number; `validate_task_schema` rejects
anything else at load time.

### Environment variables

| Variable                 | Consumer                      | Effect                                                                           |
| ------------------------ | ----------------------------- | -------------------------------------------------------------------------------- |
| `ROADMAP_MAX_ITERATIONS` | `hooks/stop_hook.sh`          | Overrides the per-session iteration cap; passed to `roadrunner check-stop` as `--max-iterations`. Defaults to `100`. See "Runaway-Protection Cap" below. |
| `ROADMAP_MAX_BUDGET_USD` | `roadrunner check-stop`       | Per-session USD budget cap. Read directly by `check-stop`; the `--max-budget-usd` flag overrides it. Feature is off when unset. See "Cost Budget Cap" below. |
| `CLAUDE_PROJECT_DIR`     | every hook script + the CLI   | Set by Claude Code itself; the CLI uses it to anchor `ROOT` (and therefore `tasks.yaml`, `logs/`, `.roadmap_state.json`). You do not set this manually unless you're invoking the CLI from outside Claude Code. |

## Iteration Counters

Roadrunner tracks two iteration counters in `.roadmap_state.json`. They exist so
the runaway-protection cap does not accidentally become a lifetime ceiling on the
project as a whole.

| Counter             | Purpose                                           | Reset on                                  |
| ------------------- | ------------------------------------------------- | ----------------------------------------- |
| `iteration`         | Lifetime audit counter. Never gates behavior.     | `reset-iteration --hard` only             |
| `session_iteration` | Per-session runaway guard. Gates the Stop-hook cap. | Every `SessionStart` hook fire, plus `reset-iteration` (soft or hard) |

Both counters are incremented on every `check-stop` invocation (i.e., every time
Claude finishes a turn and the Stop hook fires). The cap is compared against
`session_iteration` only.

### Why Two Counters?

Before 2026-04-24, Roadrunner kept a single `iteration` counter and the cap fired
when that counter hit `max_iter`. Because the counter persisted across every
`claude` invocation for the same project, long-running projects (e.g., entra-triage
running periodically against production data) would accumulate iterations across
many sessions and eventually trip the cap even though no single run looped. The
split preserves the audit trail (`iteration`) while making the runaway guard
per-session (`session_iteration`). See `logs/ROAD-010.md` for the full history.

## Runaway-Protection Cap

The Stop hook enforces a maximum number of Claude turns per session.

**Default:** 100.

**Override (env var, per invocation):**

```bash
export ROADMAP_MAX_ITERATIONS=50   # tighter cap for a short run
```

The env var is read by `hooks/stop_hook.sh` and passed as `--max-iterations` to
`roadrunner check-stop`. The CLI defaults to 100 if the flag is absent or cannot be parsed. A malformed value prints a warning to stderr.

**When the cap fires:** `check-stop` emits a hard halt with `stopReason` explaining
the cap was hit. The session terminates. The next session starts at iteration 0 when `SessionStart` resets it.

## Cost Budget Cap

ROAD-011 adds a second runaway-protection primitive that gates on dollars instead
of turns. The Stop hook reads cumulative per-session cost from Claude Code's
payload, accumulates it into `session_cost_usd` in `.roadmap_state.json`, and
hard-halts the loop when the configured cap is met or exceeded.

**Default:** off. An invalid setting prints a warning. A valid flag takes precedence over the environment variable.

**Configure (env var):**

```bash
export ROADMAP_MAX_BUDGET_USD=10.00   # halt the session at $10 of spend
```

**Configure (flag, overrides env):**

```bash
roadrunner check-stop --max-budget-usd 5.00 < payload.json
```

**When the cap fires:** `check-stop` emits the same `{"continue": false,
"stopReason": "..."}` hard-halt shape as the iteration cap, and writes a
`budget_exceeded` event to `trace.jsonl`. Run `roadrunner reset-iteration --soft`
to start a fresh session window (clears both `session_iteration` and
`session_cost_usd`).

**Graceful degradation:** if Claude Code's hook payload does not surface a cost
field that `check-stop` recognises, the feature no-ops and emits a one-time
warning to stderr — the loop never halts on a missing payload field. Recognised
shapes include `total_cost_usd`, `cost_usd`, and `session_cost_usd` at either
the top level or nested under `usage` / `cost`.

## Resetting Iteration Counters

The `reset-iteration` subcommand provides manual control.

```bash
# Reset session counter only. Lifetime counter preserved.
roadrunner reset-iteration --soft   # default if no flag

# Reset both counters. Destructive; loses lifetime audit trail.
roadrunner reset-iteration --hard
```

Trace events (`reset_iteration`) record the mode and the prior lifetime counter
for every invocation, so `--hard` is recoverable via the trace log if needed.

## State Schema

`.roadmap_state.json` is versioned via `STATE_SCHEMA_VERSION`. Roadrunner's
forward-compat contract:

- **Older Roadrunner reading newer state:** refuses to run (`version > current`
  exits with an upgrade-required message). Prevents data loss from writes that
  drop fields the newer version added.
- **Newer Roadrunner reading older state:** migrates transparently. Missing fields
  are filled in via `setdefault` during `read_state`, then written back in the
  new schema on the next `write_state`.

### Schema v1 → v2 (ROAD-010, 2026-04-24)

Adds `session_iteration: int` field. Default for legacy state files: `0`. No
manual migration required; first load auto-fills.

### Schema v2 → v3 (ROAD-011 + ROAD-014)

Adds two fields in a single bump:

- `session_cost_usd: float` — per-session cost accumulator used by the budget
  cap (see [Cost Budget Cap](#cost-budget-cap)). Default for legacy state
  files: `0.0`. Resets on every `SessionStart` fire and on `reset-iteration
  --soft`.
- `last_session_id: string | null` — most recent Claude Code session ID,
  captured from the SessionStart hook payload. Consumed by `roadrunner resume
  --session-id` to print a paste-able `claude --resume <id>` command. Default
  for legacy state files: `null`. Survives `check-stop` and `reset-iteration`
  (it points at the *prior* session, not the current one).

No manual migration required; `read_state` fills both via `setdefault` on
first load, and the next `write_state` persists the upgraded shape.

## `tasks.yaml` reference

`tasks/tasks.yaml` is the roadmap — the source of truth for task ordering,
dependencies, and validation contracts. It has two kinds of entries:
**top-level project config** and the **`tasks:` list**.

### Top-level keys

```yaml
project_base: main          # required-ish; falls back to current git branch, then 'main'
push_on_complete: base      # optional; default 'none'
tasks:                      # required; the ordered task list
  - id: ROAD-001
    # ...
```

| Key                | Type                     | Required? | Purpose                                                                                                           |
| ------------------ | ------------------------ | --------- | ----------------------------------------------------------------------------------------------------------------- |
| `project_base`     | string                   | optional  | Branch that `roadrunner/<TASK-ID>` branches fork from. Prevents stacking. Falls back to current branch, then `main`. |
| `push_on_complete` | `"base"` / `"task"` / `"both"` / `"none"` | optional | What to push after a successful merge on `complete`. Default `"none"` (local only). |
| `tasks`            | list                     | required  | Ordered task list. See field reference below.                                                                     |
| `baseline_validation` | list[string]          | optional  | ROAD-015: project-wide validation gate. `roadrunner validate` runs every command here before any task's own `validation_commands`, short-circuiting on first failure. Set this to the exact commands your CI workflow runs so the loop's validate gate is structurally equal to CI. **Language-agnostic** — see the next section. |
| `coverage_floor` | int | optional | Issue #18: minimum number of passing checks that actually verified something (default `1`, `0` disables). `true`/`echo`/`printf`/`exit`/`:` and test runs reporting `no tests ran`, `collected 0 items`, `Ran 0 tests` or `No tests found` pass but do not count; an unmet floor fails validation with a `vacuous` result so `complete` will not merge. Also settable per task; the task value wins. |

### Baseline validation (ROAD-015)

`baseline_validation` is the project-wide CI-equivalent gate. `roadrunner validate`
runs every command in this list **before** the active task's per-task
`validation_commands`. If any baseline command fails, the rest of validation
short-circuits — remaining baseline commands and all task-specific commands are
skipped, and the task cannot reach `complete`. Within the task-specific phase,
commands continue on failure so the operator still sees every task-level issue
at once.

The runtime treats each entry as an opaque shell command, so the field is
**language-agnostic**:

**Python project:**

```yaml
baseline_validation:
  - python3 -m pytest tests/ -q
  - ruff check src/ hooks/ tests/
  - python3 -m mypy src tests --ignore-missing-imports
```

**TypeScript project:**

```yaml
baseline_validation:
  - npm test
  - npm run lint
  - npx tsc --noEmit
```

When the field is absent, empty, or malformed (e.g. `baseline_validation: true`
instead of a list), the helper returns `[]` and `validate` falls back to its
pre-ROAD-015 behavior (task-specific commands only). This preserves backward
compatibility for projects that haven't opted in.

Trace events distinguish the two phases: every `validation_command` event
carries a `phase: "baseline" | "task"` field, and `validation_complete` carries
`baseline_passed` and `task_passed` booleans so external pipelines can attribute
failures correctly. The CLI output renders the two suites as separate
`── baseline ──` / `── task ──` blocks for the same reason.

The motivation is **runtime enforcement of local↔CI parity**: CLAUDE.md
conventions are suggestion-only and drift across long sessions; only code paths
are deterministic. Without `baseline_validation`, an individual task's
`validation_commands` can omit a check that CI runs (this happened on
2026-05-28 — `mypy src/roadrunner` locally, `mypy src tests` in CI — and shipped
two consecutive red CI runs). With `baseline_validation` set to CI's exact
commands, that gap is structurally impossible.

### Per-task fields

Required on every task:

| Field    | Type   | Notes                                                       |
| -------- | ------ | ----------------------------------------------------------- |
| `id`     | string | Must match `^[A-Z]+-\d+$` (e.g., `ROAD-001`, `TASK-042`).   |
| `title`  | string | Short, human-readable title. Used in briefs and status output. |
| `status` | string | One of `todo`, `in_progress`, `done`, `blocked`.             |

Optional on every task:

| Field                   | Type           | Default       | Notes                                                                                                  |
| ----------------------- | -------------- | ------------- | ------------------------------------------------------------------------------------------------------ |
| `depends_on`            | list[string]   | `[]`          | Task IDs that must be `done` before this task becomes eligible. Must be a list; cycles are rejected by `roadrunner analyze`. |
| `goal`                  | string         | `""`          | Prose description. Injected into the task brief at session start / resume.                             |
| `acceptance_criteria`   | list[string]   | `[]`          | Bullet list shown in the brief. Advisory — not checked programmatically.                               |
| `validation_commands`   | list[string]   | `[]`          | Shell commands run by `roadrunner validate`. All must exit 0 for `complete` to succeed.                |
| `validation_timeout`    | number         | `DEFAULT_VALIDATION_TIMEOUT` (300) | Per-task override in seconds. Must be a positive number.                       |
| `files_expected`        | list[string]   | `[]`          | Paths the task is allowed to touch. Used by `roadrunner commit` to scope staged changes.               |
| `documentation_targets` | list[string]   | `[]`          | Documentation files that should also be updated. Advisory — informational only.                        |
| `notes`                 | string         | `""`          | Free-form notes. Not interpreted by the control loop.                                                   |
| `model`                 | string         | none          | ROAD-012: optional per-task model hint (e.g. `claude-haiku-4-5`). Surfaced in `status`, `next`, the resume brief, and every `check_stop` trace event so future analysis can correlate model choice with task outcome. Roadrunner itself does **not** route turns to a different model — operators who want enforcement wrap `claude` in a script that reads the field. Unknown IDs warn once per process, never block loading. |

Schema validation (`validate_task_schema`) runs on every `load_tasks`. It
rejects: missing required fields, invalid `id` format, unknown `status`,
non-list `depends_on` or `validation_commands`, and non-positive
`validation_timeout`.

### Minimal example

```yaml
project_base: main
tasks:
  - id: TASK-001
    title: "First task — replace me"
    status: todo
    depends_on: []
    goal: |
      Describe what this task accomplishes.
    acceptance_criteria:
      - "Describe a concrete, observable outcome"
    validation_commands:
      - "echo 'replace with a real check (test, lint, build)'"
    files_expected: []
```

---

## `.roadmap_state.json`

The control-loop state file. Mutated on every `start`, `complete`, `block`,
`reset`, and `check-stop`. Protected by an advisory POSIX lock
(`.roadmap_state.lock`) and written atomically via `os.replace`.

Schema v3 (current):

```json
{
  "schema_version": 3,
  "current_task_id": "ROAD-011",
  "iteration": 117,
  "session_iteration": 2,
  "session_cost_usd": 1.42,
  "last_session_id": "claude-session-abc123",
  "attempts_per_task": {
    "ROAD-001": 3,
    "ROAD-005": 5
  },
  "updated_at": "2026-05-27T22:00:00.000000+00:00",
  "base_branch": "roadrunner/ROAD-011"
}
```

| Field               | Type            | Purpose                                                                                |
| ------------------- | --------------- | -------------------------------------------------------------------------------------- |
| `schema_version`    | int             | Matches `STATE_SCHEMA_VERSION` (currently `3`). Controls forward-compat gate.           |
| `current_task_id`   | string \| null  | ID of the task currently `in_progress`, or null between boundaries.                    |
| `iteration`         | int             | Lifetime audit counter. Never gates behavior. Only reset via `reset-iteration --hard`. |
| `session_iteration` | int             | Per-session runaway-protection counter. Reset every `SessionStart` fire and via `reset-iteration`. Gates the iteration cap. |
| `session_cost_usd`  | float           | Per-session USD cost accumulator. Reset every `SessionStart` fire and via `reset-iteration --soft`/`--hard`. Gates the budget cap (ROAD-011). |
| `last_session_id`   | string \| null  | Most recent Claude Code session ID captured by SessionStart. Read by `roadrunner resume --session-id`/`--exec` (ROAD-014). Survives `check-stop` and `reset-iteration`. |
| `attempts_per_task` | dict[str, int]  | Count of resume cycles per task. Reaching `MAX_TASK_ATTEMPTS` (5) auto-blocks the task. |
| `updated_at`        | ISO-8601 string | UTC timestamp of the last write.                                                       |
| `base_branch`       | string          | The branch in play when the last `start` ran — used to recover the base branch after task merges. |

## `.context_snapshot.json`

Written by the PreCompact hook (`roadrunner snapshot`) so Claude can cold-resume
after context compaction. Verified by the PostCompact hook
(`roadrunner post-compact`), which logs a `post_compact_verify` trace event.

Schema v1 (current):

```json
{
  "schema_version": 1,
  "snapshot_at": "2026-04-25T02:11:50.665330+00:00",
  "current_task": "ROAD-005",
  "iteration": 109,
  "next_eligible": "ROAD-006",
  "status_summary": {
    "ROAD-001": "done",
    "ROAD-002": "done",
    "ROAD-005": "in_progress",
    "ROAD-006": "todo"
  }
}
```

| Field            | Type            | Purpose                                                                 |
| ---------------- | --------------- | ----------------------------------------------------------------------- |
| `schema_version` | int             | Matches `SNAPSHOT_SCHEMA_VERSION` (currently `1`).                      |
| `snapshot_at`    | ISO-8601 string | UTC timestamp of the snapshot write.                                    |
| `current_task`   | string \| null  | ID of the `in_progress` task at snapshot time, if any.                   |
| `iteration`      | int             | Lifetime iteration counter at snapshot time.                            |
| `next_eligible`  | string \| null  | ID of the next eligible `todo` task, if any.                            |
| `status_summary` | dict[str, str]  | Flat `id → status` map covering every task in `tasks.yaml`.             |

PostCompact verification checks that every required field is present and that
`schema_version == SNAPSHOT_SCHEMA_VERSION`. Mismatches surface as
`schema_mismatch` / `missing_fields` keys in the `post_compact_verify` trace
event.

---

## Downstream Consumers

`entra-triage` vendors `roadrunner.py` in-tree and syncs periodically. Because the
v1 → v2 migration is setdefault-based (not a hard schema check), vendored copies
pick up the new schema without coordination: the state file on disk migrates the
first time a post-sync `roadrunner.py` invocation runs.

If you fork or vendor this file elsewhere, follow the same contract:

1. Bump `STATE_SCHEMA_VERSION` when adding new state fields.
2. In `read_state`, put `data.setdefault(new_field, default)` after the JSON load
   so older state files migrate cleanly.
3. Keep the forward-compat gate (`version > STATE_SCHEMA_VERSION → exit`) intact
   so older binaries can't silently corrupt newer state.
