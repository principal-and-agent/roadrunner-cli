# Roadrunner operator notes

Use this page when a run stops, a task has no clear next action, or an interactive Claude session behaves unexpectedly.

Start with the [operator guide](https://github.com/principal-and-agent/roadrunner-cli/blob/main/docs/WORKFLOW.md) for setup and a first run. The [configuration reference](https://github.com/principal-and-agent/roadrunner-cli/blob/main/docs/configuration.md) lists policy fields and defaults.

## A supervised run stopped

1. Capture the run ID and inspect the task's `phase`, `reason`, and attempt count in the JSON report. If the command failed before printing a report, start with stderr.
2. Check the run branch from the report with `git log HEAD..BRANCH --oneline` and `git diff HEAD BRANCH`, substituting its branch name. This shows what was integrated while leaving your checkout in place.
3. Find the controller records using the commands below. Set `RUN_ID` to the report's `run_id`. Use `task_work_root` from `run.json` to locate the worktrees, and keep them intact during recovery.

```sh
RUN_ID=REPLACE_WITH_RUN_ID
CONTROL_DIR="$(git rev-parse --path-format=absolute --git-common-dir)/roadrunner"
ls "$CONTROL_DIR/$RUN_ID"
cat "$CONTROL_DIR/$RUN_ID/run.json"
```

For an interrupted run, resume with `roadrunner run --resume "$RUN_ID"`. For terminal blockers, fix the listed cause, review the plan, and launch a new run. The original limits and charged attempts remain in effect when resuming.

## A task cannot proceed

| Symptom | Where to look |
| --- | --- |
| `sync` reports `needs_input` | Supply a goal, measurable acceptance criteria, and repository-relative file scope. For issue intake, add those fields in the reviewed rules file. |
| `sync` reports `needs_review` | Review the changed source and set `approved_revision` to the revision returned by sync. |
| `doctor` reports an adapter or sandbox failure | Check that the same shell can find the authenticated Claude CLI and macOS `sandbox-exec`. Run doctor with the policy you intend to use. |
| An attempt fails validation | Read the validation output, confirm dependencies were prepared before the run, and check that the gate works without network access. Keep the gate and its fixtures in `protected_paths`. |
| Delivery stops while waiting for checks | Compare the recorded PR head and base, and inspect the exact check names in the delivery policy. If the remote base changed, rebuild and revalidate before retrying. |

## The interactive Claude hooks behave unexpectedly

- **No brief or repeated stop.** Check `roadrunner status` and `roadrunner next`. Confirm the project settings register SessionStart and Stop, especially when `init` preserved an existing `.claude/settings.json`. Check whether the loop is paused.
- **Claude cannot edit the roadmap.** The project guard denies Claude Write and Edit on `tasks/tasks.yaml`. Use the task CLI for status changes and have the operator review definition changes.
- **Completion says validation changed.** Restore the validation settings pinned at start. Review the task and its branch before restarting with new validation settings.
- **An iteration cap halted the session.** `roadrunner reset-iteration --soft` resets the session counter and preserves the lifetime counter. `roadrunner resume --session-id` prints the last recorded Claude session command if one was captured.

## Report a problem

Include the workflow, command, Roadrunner revision, operating system, and a minimal reproduction. For the supervised workflow, include the run ID, phase, and relevant attempt output. For the interactive loop, include the relevant lines from `logs/trace.jsonl`. Redact credentials and project data. Use the [issue tracker](https://github.com/principal-and-agent/roadrunner-cli/issues) for bugs and the [security policy](https://github.com/principal-and-agent/roadrunner-cli/blob/main/SECURITY.md) for vulnerabilities.
