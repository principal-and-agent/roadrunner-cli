# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> The internal per-task audit trail lives in [`logs/CHANGELOG.md`](logs/CHANGELOG.md);
> this file is the release-facing summary.

## Unreleased

### Added

- **Supervised local delivery** — freeze a reviewed plan with `sync`, preflight with `doctor`, and execute tasks in isolated worktrees with `run`. The controller validates and integrates changes into a dedicated run branch while preserving the operator checkout.
- **Optional GitHub delivery** — prepare a delivery artifact locally or explicitly execute the permitted operations after review. Live remote writes have yet to be qualified.
- **Interactive roadmap guard** — the project and the installed scaffold block Claude Write and Edit calls targeting `tasks/tasks.yaml` ([#48](https://github.com/principal-and-agent/roadrunner-cli/pull/48)).

- **Coverage floor (#18)** — a task no longer passes validation on checks that
  verified nothing. Commands that are bare no-ops (`true`, `echo`, ...) or test
  runs that report an empty run are marked `vacuous` and do not count toward
  `coverage_floor` (default 1, set in `tasks.yaml` top level or per task; `0`
  disables). **Behaviour change:** a task whose only validation is `true`/`echo`
  now fails with a `vacuous` result; set `coverage_floor: 0` to keep the old behaviour.
- **Project-wide baseline validation suite (ROAD-015)** — `tasks.yaml` accepts
  an optional top-level `baseline_validation: list[str]` field. `roadrunner
  validate` runs each command in this list before the active task's own
  `validation_commands`, short-circuits on first failure (the task-specific
  phase is skipped entirely if any baseline command fails), and continues on
  failure within the task-specific phase. Each `ValidationResult` carries a
  `phase` field (`baseline` / `task`); `validate` output renders the two
  suites as distinct blocks; `validation_command` trace events carry the
  phase, and `validation_complete` carries `baseline_passed` / `task_passed`
  booleans. The runtime treats commands as opaque shell invocations, so the
  field is **language-agnostic** — Python projects set pytest/ruff/mypy,
  TypeScript projects set npm/eslint/tsc. This project's `tasks.yaml` is now
  configured with the three CI-mirror commands, making local↔CI parity
  structural rather than conventional (closes the 2026-05-28 drift incident).
  See `docs/configuration.md` § Baseline validation. Backward-compat: when
  the field is absent/empty/malformed, behavior is unchanged.
- **`roadrunner resume --session-id` (ROAD-014)** — the SessionStart hook now
  captures Claude Code's `session_id` from the hook payload into
  `.roadmap_state.json` as `last_session_id`. The existing `roadrunner resume`
  subcommand grows two mutually-exclusive flags: `--session-id` prints a
  paste-able `claude --resume <id>` command using the captured ID, `--exec`
  runs it directly. Without flags, `resume` keeps its existing pause-toggle
  semantics. `roadrunner status` surfaces the captured ID when one exists.
  Schema field `last_session_id: string | null` rides along with ROAD-011's
  v2 → v3 bump (no second migration); legacy state files auto-fill `null`.
  `check-stop` and `reset-iteration` preserve the value — it points at the
  *prior* session, not the current one, so it must survive cap resets. See
  `docs/configuration.md` § Per-task fields and the schema v3 section.
- **Operator learnings log (ROAD-013)** — `logs/learnings.md` is a new
  append-only journal of non-obvious project facts the agent discovers during
  task execution (unusual build commands, flaky tests, env vars that must be
  set, files that moved). The SessionStart hook reads the last 20 entries and
  surfaces them in `additionalContext` so lessons persist across context
  compaction and across `claude` restarts. `roadrunner status` reports the
  entry count; `roadrunner init` scaffolds the file with a header in new
  projects. Both the project's `CLAUDE.md` and the init scaffold's CLAUDE.md
  template instruct the agent to append a one-line entry whenever it
  discovers something worth keeping. See `docs/WORKFLOW.md` § Operator
  Learnings Log for the full contract.
- **Per-task model hint (ROAD-012)** — `tasks.yaml` accepts an optional
  `model:` string field per task (e.g. `claude-haiku-4-5`, `claude-opus-4-7`).
  Surfaced inline in `roadrunner status` (`[model]` suffix), above the goal in
  `roadrunner next` and the Stop-hook resume brief, and on every `check_stop`
  trace event for the active task so trace.jsonl readers can correlate model
  choice with outcomes. `roadrunner analyze` reports a "Tasks by model"
  breakdown when at least one task carries the field. Roadrunner itself does
  not route turns to a different model — the field is a hint operators read
  from a `claude`-wrapping script. Type-strict (non-empty string when present);
  value-permissive (unknown IDs warn once per process, never error) so a
  newly-released model never breaks loading. See `docs/configuration.md`
  § Per-task fields for the contract.
- **Cost budget halt (ROAD-011)** — `roadrunner check-stop` now enforces a
  per-session USD budget alongside the existing iteration cap. Configure via
  `ROADMAP_MAX_BUDGET_USD` env var or the new `--max-budget-usd` flag; flag
  wins on conflict. Overruns emit the same `{"continue": false, "stopReason":
  ...}` hard-halt shape as the iteration cap and record a `budget_exceeded`
  trace event. `roadrunner status` and `roadrunner health` surface current
  spend + configured cap. State schema bumped v2 → v3 to add the new
  `session_cost_usd` field; legacy state files auto-migrate via `setdefault`
  (default `0.0`). Feature is off by default; degrades gracefully with a
  one-time stderr warning when Claude Code's payload does not surface cost
  data. See `docs/configuration.md` § Cost Budget Cap for the full contract.

### Removed

- **`requirements.txt`** — deleted the four-line backward-compat shim
  (`-e .[dev]`). All install instructions now point directly to
  `pip install -e .[dev]` (full dev setup) or `pip install -e .` (runtime
  only). CI workflows updated to use `cache-dependency-path: pyproject.toml`.

### Changed

- **Documentation cleanup for v1.0 ship** — `README.md` 358→176 lines,
  `CONTRIBUTING.md` 239→108, `docs/architecture.md` 449→159 by linking
  out to `docs/` for deep-dive content instead of duplicating it. The
  `architecture.md` April-15 risk register (all items "FIXED") removed —
  current risks track via ADRs and `docs/hotfix-log.md`. The stale "copy-in
  vs external runner" embedding guidance and resolved open questions also
  dropped from `architecture.md` (pip install + `roadrunner init` is the
  shipping path). The PostCompact hook contract added to `architecture.md`
  §2.4 (hook shipped in ROAD-005 but was undocumented).
- **Frozen historical reviews moved to `docs/history/`** —
  `code-review-audit.md` and `architecture-review-2026-04-15.md` now live
  under `docs/history/` with a "historical snapshot" banner so readers
  don't mistake them for current design docs. Their internal `DESIGN.md`
  references are preserved as part of the original record.
- **Documentation layout** — `DESIGN.md` moved to `docs/architecture.md`.
  Reduces the root file count and groups architectural reference
  material with the rest of the design docs (ADRs, configuration,
  workflow). Active links in `README.md`, `CONTRIBUTING.md`,
  `docs/WORKFLOW.md`, and `hooks/README.md` updated.

### Fixed

- **Interactive loop state and validation** — pin validation from start through completion, serialize task transitions with Stop, warn and recover from malformed session limits, publish work files atomically, and refuse to block a completed task ([#40](https://github.com/principal-and-agent/roadrunner-cli/issues/40)).

- Restored the original roadrunner banner at the top of the repository README.
- Stale references to `docs/resolution-plan-2026-04-24.md` (file no
  longer in the working tree) scrubbed from
  `.github/workflows/weekly-smoke.yml` and `src/roadrunner/session.py`
  module docstring. ADR-011's reference is left intact — it already
  hedges with "when present in working tree" and ADRs are append-only.
- **Package layout** — moved the flat-module trio (`roadrunner.py`,
  `rr_state.py`, `rr_session.py`) into a proper `src/roadrunner/` package
  (`cli.py`, `state.py`, `session.py`) with `__init__.py` and
  `__main__.py`. The `roadrunner` console script and `python -m roadrunner`
  are the canonical entry points; the file-based fallback the hooks used
  to honour (`python3 $PROJECT_ROOT/roadrunner.py`) is gone. `import
  roadrunner; roadrunner.foo` and `monkeypatch.setattr(roadrunner, ...)`
  continue to work via a `sys.modules` alias to the cli module so legacy
  callers and tests are unaffected.
- Path resolution — `ROOT` and `STATE_FILE`/`STATE_LOCK` now resolve from
  `CLAUDE_PROJECT_DIR` (set by the hooks) with a `cwd()` fallback,
  replacing the old `Path(__file__).parent` anchor that worked only when
  the file lived at the project root.

## 1.0.0 - 2026-04-25

### Added

- **`roadrunner watch`** — read-only live monitor that polls disk state on a
  configurable interval and renders a status frame (iteration, active task,
  task counts, last 5 trace events, elapsed time). Stdlib only; clean Ctrl-C
  exit. (ROAD-007)
- **PostCompact hook** — fires after Claude Code completes context compaction;
  verifies `.context_snapshot.json` survived and logs a `post_compact_verify`
  trace event with trigger and compact summary. (ROAD-005)
- **PyPI publish workflow** — `.github/workflows/publish.yml` triggers on
  `v*` tag pushes (and via `workflow_dispatch` for rehearsal). Uses OIDC
  Trusted Publishing through `pypa/gh-action-pypi-publish` — no API tokens
  in repo secrets. See [`docs/release.md`](docs/release.md) for setup.
  (ROAD-008)
- **`docs/examples/hello-roadrunner/`** — end-to-end three-task worked
  example (word-counter CLI) so new users can copy → `roadrunner init` →
  `claude` and watch the loop complete a real demo. (ROAD-009)
- **`CONTRIBUTING.md`** at the repo root — dev setup, tests/lint/CI gate, PR
  workflow, and recipes for adding subcommands and hooks. (ROAD-006)
- **`docs/configuration.md`** extended — full tunables table, env vars,
  `tasks.yaml` field reference, and on-disk schemas for both state files.
  (ROAD-006)
- **Per-session iteration counter** (`session_iteration`) with reset on every
  `SessionStart` hook fire; gates the runaway-protection cap so long-lived
  projects don't accumulate iterations across sessions. State schema bumped
  to v2 with backward-compatible `setdefault` migration. (ROAD-010)
- **`build>=1.0`** added to dev dependencies for local release rehearsal.
- **Top-level `LICENSE` file** (MIT, matching `pyproject.toml`).
- **Top-level `CHANGELOG.md`** (this file).

### Changed

- README operator-commands list refreshed to include `init`, `analyze`,
  `commit`, `reset-iteration`, `watch`, and `post-compact`.
- README architecture block now lists `PostCompact` alongside the other
  hooks.
- README install/usage docs reference the published `pip install
  roadrunner-cli` + `roadrunner` console-script entry point.

### Fixed

- Repo URLs corrected to match the actual GitHub remote
  `afoxnyc3/roadrunner-cli` (pyproject.toml previously had a typo'd
  `afoxnyc/...`; README, CONTRIBUTING.md, and docs/release.md aligned).
- `.gitignore` now covers `build/`, `dist/`, `*.egg-info/`, `.mypy_cache/`,
  and `.ruff_cache/`.

## [0.0.1] — pre-release

Initial development. See `git log` for full history before the changelog
was started.
