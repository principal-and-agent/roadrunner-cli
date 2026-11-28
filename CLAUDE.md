# CLAUDE.md — Interactive roadmap agent brief

## Operating Model

These instructions apply when operating this repository through the interactive Claude hooks. The supervisor supplies its own frozen plan and task brief. Python owns task transitions, and you implement the active task.
**One task per cycle. No side quests. No skipping ahead.**

---

## Your Job Each Cycle

1. Run `roadrunner next` to see the current task
2. Run `roadrunner start TASK-XXX` before touching any files
3. Implement the task — stay within its scope
4. Run `roadrunner validate TASK-XXX` to check your work
5. Fix any failures — validation commands are the source of truth, not your assessment
6. Commit the scoped implementation with `roadrunner commit TASK-XXX` before completion.
7. Run `roadrunner complete TASK-XXX --notes "what you did"` to close the task

**The Stop hook will determine what comes next. Do not attempt to decide task order yourself.**

---

## Completion Signal

When ALL tasks are `done` and there is no remaining eligible work:

Output this exact string on its own line (must be the last non-empty line of your message):

```
ROADMAP_COMPLETE
```

This halts the loop. Do not output it unless the roadmap is genuinely finished.
Do not quote it mid-sentence — the signal is line-anchored and only triggers when it appears alone on the final line.

---

## Blocked Tasks

If a task cannot be completed due to an unresolvable dependency or external blocker:

```bash
roadrunner block TASK-XXX --notes "reason for block"
```

Document what is blocking. Do not keep retrying indefinitely.

**Auto-block:** If the Stop hook detects you have resumed the same in-progress task 5 times without completing it, the task is automatically blocked. You will be notified when this happens.

---

## Validation Is the Gate

Validation is necessary. Completion also requires the active task, satisfied dependencies, and successful Git integration. Start pins the validation definition, and complete reruns it before integration. The roadmap guard denies Claude Write and Edit operations on `tasks/tasks.yaml`. Use the CLI for status changes.

---

## Context Hygiene

- Work on exactly one task per cycle
- Do not reference prior task details unless directly relevant to current task
- After each `complete`, treat the prior task as closed
- On session start, the SessionStart hook builds a brief from the live roadmap
- The `.context_snapshot.json` and `.roadmap_state.json` are your memory — read them if the snapshot seems stale
- When you discover a non-obvious project fact during a task (unusual build command, flaky test, env var the script silently requires, file that moved), append a one-line entry to `logs/learnings.md` in the form `YYYY-MM-DD — terse fact`. The SessionStart hook surfaces the last 20 entries to the next session. Append-only; never edit prior entries.

---

## File Scope

Only touch files listed in the current task's `files_expected` and `documentation_targets`.
If you need to modify something outside scope, note it in the task's `--notes` and continue.

---

## Commands Reference

```bash
roadrunner status                        # show all task statuses
roadrunner next                          # show next eligible task
roadrunner start TASK-XXX               # begin a task
roadrunner validate TASK-XXX            # run validation only
roadrunner complete TASK-XXX --notes "" # complete with validation
roadrunner block TASK-XXX --notes ""    # mark blocked
roadrunner reset TASK-XXX --summary ""  # write boundary marker
roadrunner health                        # system health check
```

---

## What the Stop Hook Does

After every response, the Stop hook checks (in this order):

1. Python evaluates continuation, including `stop_hook_active`; the shell wrapper does not bypass it.
2. Is `.roadrunner_paused` present in the project root? → allow stop (ad-hoc bypass)
3. Iteration limit reached? → hard stop with message
4. Did you output `ROADMAP_COMPLETE` as the last line AND are all tasks done? → allow stop
5. Is a task `in_progress`? → inject a resume brief and block stop
6. Are there eligible `todo` tasks? → inject the next task brief and block stop
7. Are tasks blocked? → report blocked tasks and block stop
8. Are all tasks done? → prompt you to output `ROADMAP_COMPLETE`

**Auto-block guard:** If you resume the same in-progress task 5+ times without completing it, the hook auto-blocks the task and tells you to move on.

**Pause bypass:** `roadrunner pause` writes `.roadrunner_paused`; while it exists the hook short-circuits to `exit 0` so you can do ad-hoc work without the loop hijacking the session. `roadrunner resume` removes it. This is the supported way to bypass the loop — do not move `.claude/settings.json` aside.

You do not need to manage this. The hook manages it. Just do the work.
