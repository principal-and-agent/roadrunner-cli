# CLAUDE.md — hello-roadrunner demo brief

You are executing a deterministic three-task roadmap. Python owns control.
You own implementation. **One task per cycle. No side quests. No skipping ahead.**

## Each cycle

1. `roadrunner next` — see the next eligible task.
2. `roadrunner start DEMO-XXX` — creates the task branch, marks `in_progress`.
3. Implement the task. Stay strictly inside the task's `files_expected`.
4. `roadrunner validate DEMO-XXX` — every `validation_command` must exit 0.
5. Commit the task changes with `roadrunner commit DEMO-XXX`.
6. `roadrunner complete DEMO-XXX --notes "what you did"` — marks done.
6. `roadrunner reset DEMO-XXX --summary "one-line"` — boundary marker.

The Stop hook decides what comes next. Do not pick task order yourself.

## Completion signal

When all three tasks are `done` and `roadrunner next` reports nothing
eligible, output the sentinel `ROADMAP_COMPLETE` on its own line as the
last non-empty line of your message. This halts the loop cleanly.

## File scope

Only touch files in the current task's `files_expected`. The whole demo
project lives in three files: `word_counter.py` and `test_word_counter.py`
get created as you go; `tasks/tasks.yaml` defines the roadmap and you
should not edit it.

## Validation is the gate

A task is done after `roadrunner complete DEMO-XXX` reruns the pinned checks, integrates the branch, and records the completed status.
