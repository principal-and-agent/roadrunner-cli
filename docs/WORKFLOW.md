# Operator guide

Roadrunner's supervised workflow starts with a reviewed backlog and produces a local Git branch and a JSON report. The interactive Claude hook workflow is covered [below](#claude-hook-workflow).

## First supervised run

Use an authenticated Claude Code CLI on macOS with `sandbox-exec`. Install Roadrunner as described in the [README](../README.md). Run the walkthrough in a disposable Git project so you can inspect the outcome before applying the same steps to your own repository.

```sh
mkdir roadrunner-first-run
cd roadrunner-first-run
git init -b main
```

Save this as `roadmap.yaml`:

```yaml
tasks:
  - id: DOC-001
    title: Add a greeting
    goal: Create greeting.txt containing one line with the text Hello, Roadrunner!
    acceptance_criteria:
      - greeting.txt contains exactly one line with the expected greeting.
    files_expected:
      - greeting.txt
```

Save the following as `verify.py`. It exits nonzero until the expected file exists with the correct contents.

```python
from pathlib import Path

assert Path("greeting.txt").read_text(encoding="utf-8").splitlines() == ["Hello, Roadrunner!"]
```

Save this reviewed policy as `run-policy.json`:

```json
{
  "adapter": "claude",
  "sandbox": "macos",
  "delivery": "local",
  "validation_commands": ["python3 verify.py"],
  "protected_paths": ["verify.py"],
  "max_attempts": 2,
  "attempt_seconds": 120,
  "validation_seconds": 30,
  "run_seconds": 300,
  "max_budget_usd": 2.0,
  "attempt_budget_usd": 0.5
}
```

```sh
git add roadmap.yaml verify.py run-policy.json
git commit -m "Add reviewed Roadrunner demo plan and gate"
roadrunner sync --roadmap roadmap.yaml
roadrunner doctor --policy run-policy.json
```

Review the `items` and `plan` fields in the synchronization report, then run:

```sh
roadrunner run --plan /absolute/path/from-sync.yaml --policy run-policy.json
```

A run invokes Claude and can incur provider costs. Verify a successful run by copying the branch from the report:

```sh
RUN_BRANCH=roadrunner/run-REPLACE_WITH_RUN_ID
git diff HEAD "$RUN_BRANCH" -- greeting.txt
git show "$RUN_BRANCH:greeting.txt"
```

Set `RUN_BRANCH` to the report’s `branch` value. Git accepts the full `refs/heads/` prefix included in the report. Review the integrated changes before adopting them. For an offline fixture-based check of the installed CLI, see [Contributing](../CONTRIBUTING.md#installed-package-check).

## Select work from an existing backlog

Use `sync --roadmap PATH` for YAML or a Markdown file with a single fenced YAML block containing `tasks`. For GitHub selection, authenticate the `gh` CLI and pass either explicit issue numbers or label and milestone filters:

```sh
roadrunner sync --github OWNER/REPO --issue 123 --issue 124 --rules intake-rules.json
roadrunner sync --github OWNER/REPO --label ready --milestone 2 --rules intake-rules.json
```

Review the source disposition of each item before running. `needs_input` asks you to supply missing scope or acceptance criteria. `needs_review` asks you to approve an edited or reopened source. A closed external issue remains distinct from verified Roadrunner resolution. The [intake rules reference](configuration.md#github-intake-rules) shows how to supply reviewed values.

## Read, recover, or stop a run

| Outcome | What to inspect |
| --- | --- |
| `resolved` | The task's evidence and the diff on the run branch. |
| `needs_input` | The reported reason and the source plan or reviewed intake rules. |
| `blocked` or `failed` | The task's prerequisites, attempt logs, validation output, and remaining allowances. |
| `cancelled` | The report and any work already integrated before cancellation. |

Exit code 0 means every selected task resolved locally. Exit code 2 can indicate blockers, cancellation, or a command error. Read the JSON report when emitted and stderr for preflight or invocation errors.

Cancel with Ctrl-C or SIGTERM. To reconcile an interrupted run, use `roadrunner run --resume RUN_UUID` from the project. Resume preserves the original deadline, attempt charges, and budget reservations. To retry terminal blockers after fixing inputs, select a new run explicitly.

Use `git rev-parse --path-format=absolute --git-common-dir` to find the Git common directory. The run record is under `roadrunner/RUN_UUID/run.json` inside it. Find the temporary task worktrees through `task_work_root` in that record. Preserve this directory while inspecting or recovering interrupted work.

For specific symptoms and log locations, see the [operator notes](operator-notes.md).

## Optional GitHub delivery

```sh
roadrunner deliver --run RUN_UUID --policy delivery-policy.json
```

This prepares a local delivery artifact. Remote writes require `--execute` and the corresponding explicit capabilities in the [delivery policy](configuration.md#delivery-policy). Delivery checks exact PR identity, required checks, and merged tree evidence before optionally reconciling source issues. Live GitHub writes have yet to be qualified. See the [qualification checklist](shipping-implementation-checklist.md).

## Claude hook workflow

Use this mode for an interactive Claude session with project-level Roadrunner hooks. Run `roadrunner init .` and review `CLAUDE.md` and `.claude/settings.json`. Existing files are preserved, so reconcile existing hook settings manually when adding Roadrunner to an established project. Read [the hook contracts](architecture.md#2-hook-contracts) before changing hook behavior.

1. Review `tasks/tasks.yaml` and run `roadrunner analyze`. The operator owns roadmap definitions.
2. Use `roadrunner next` and `roadrunner start TASK-001` to select the next eligible task and prepare its branch. Start pins the validation definition.
3. Implement the task in scope. Use `roadrunner validate TASK-001` to check it and `roadrunner commit TASK-001` to commit task changes.
4. Run `roadrunner complete TASK-001 --notes "Summary"`. Completion reruns the pinned checks and integrates the task before marking it done. For work that needs help, use `roadrunner block TASK-001 --notes "What is needed"`.
5. In an interactive session, the Stop hook supplies the next task or resumes active work. The final `ROADMAP_COMPLETE` sentinel works after every task is done.

The PreToolUse hook denies Claude `Write` and `Edit` operations targeting this project's roadmap. Use the CLI for status transitions. See the [hook configuration](configuration.md#claude-hook-configuration) for validation settings, iteration limits, and state files.

| Need | Command or file |
| --- | --- |
| Read status, counters, or the next task | `roadrunner status`, `roadrunner watch`, `roadrunner next` |
| Pause or re-enable the Stop hook | `roadrunner pause`, `roadrunner resume` |
| Recover the last recorded Claude session | `roadrunner resume --session-id` |
| Reset the session iteration allowance | `roadrunner reset-iteration --soft` |
| Read task history | `logs/<TASK-ID>.md`, `logs/trace.jsonl`, `roadrunner sessions` |
| Carry an operational lesson into future Claude sessions | Add a concise entry to `logs/learnings.md`. |

See [hello-roadrunner](examples/hello-roadrunner/README.md) for the three-task interactive example.
