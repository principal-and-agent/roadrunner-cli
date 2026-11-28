# Review rules: Roadrunner

Read by Anthropic's Code Review on GitHub and by the `review-gate` plugin in
Claude Code. CONTRIBUTING.md describes the project; this file says what a
reviewer should flag.

## What Important means here

Report as Important only issues that would cause one of these if merged:

- **Loop control breaks.** A Stop hook path that lets Claude stop when work
  remains, blocks forever, bypasses the iteration cap, per-task auto-block, or
  `.roadrunner_paused`, or emits both `decision` and `continue: false` in one
  payload. `cmd_check_stop` in `cli.py` is the source of truth for how
  `stop_hook_active` is handled. See the [hook contracts](docs/architecture.md#2-hook-contracts).
- **State corruption.** A write to `.roadmap_state.json` that skips
  `STATE_LOCK` or isn't atomic. A change that makes any state, snapshot or
  `tasks.yaml` write less safe than it is today. A format change without
  bumping its version constant (`STATE_SCHEMA_VERSION` in `state.py`,
  `SNAPSHOT_SCHEMA_VERSION` in `cli.py`). Also flag run state that loses attempt charges, worker evidence, or the frozen policy during recovery.
- **Git or delivery damage.** Code in `delivery.py` or `cli.py` that can push,
  merge, or delete the wrong branch or commit, skips `--match-head-commit`, or
  treats a failed push or merge as success. Preserve the operator checkout and require verified evidence on the integrated commit.
- **Validation bypass.** A task can reach `done` without `roadrunner validate`
  passing through completion, or a supervised task resolves without policy validation and integration evidence.
- **Unsafe shell-out.** Task IDs, notes, branch names, or other values from
  `tasks.yaml` reach a shell without quoting, or a new code path passes
  `tasks.yaml` content to `shell=True`. The existing `validation_commands`
  runner is the intended shell entry point in the hook loop. Supervised validation takes its commands from the separately reviewed policy. Flag any import path that lets a source issue choose executable gates.
- **Informational hooks that gate.** `SessionStart`, `PostToolUse`, `PreCompact` or
  `PostCompact` hooks that exit nonzero or print decision JSON. Stop controls the loop and PreToolUse guards the project roadmap.
- **Tests that don't test.** A bug fix with no regression test, or a test that
  would pass on `main` without the change.
- **New runtime dependency.** Anything beyond the stdlib and PyYAML outside the `[dev]` extras.
- **CI gap.** Code that passes `mypy src` but fails `mypy src tests --ignore-missing-imports`.

Everything else is a Nit.

## Cap the nits

Report at most three Nits per review. Skip Nits entirely on changes under 20 lines.

## Do not report

- Formatting, import order, and line length. `ruff` owns these.
- Changes under `logs/`, `.reset_*`, `.context_snapshot.json`, or `CHANGELOG.md`.
- Missing support for Windows or Python below 3.10. Neither is supported.

## Always check

- Task work stays inside the task's `files_expected` and
  `documentation_targets`, plus the commit overlay defined by
  `_COMMIT_OVERLAY_PREFIXES` in `cli.py` (`logs/`, `tasks/tasks.yaml*`, `.reset_*`).
- New logic in hook scripts (`hooks/*.sh` and their copies in
  `src/roadrunner/scaffold/hooks/`) keeps core control logic in Python.
- New subcommands are registered at the console entry point and tested through that entry point or the relevant handler.
- New `subprocess` and `git` calls handle a non-zero exit and set a timeout.
- Renames and schema changes leave no compatibility aliases unless the schema
  is versioned. Exception: `tasks.yaml` is unversioned and user-authored, so
  existing keys must keep working.
