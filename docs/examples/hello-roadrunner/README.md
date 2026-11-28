# hello-roadrunner

A three-task example for the [interactive Claude hook workflow](../../WORKFLOW.md#claude-hook-workflow). Claude adds a word-counting function, a command-line entry point, and tests in dependency order.

## Run it

Install Roadrunner from source using the [repository README](../../../README.md). With the same virtual environment active, run the following from the repository root. Choose a new demo directory.

```sh
python -m pip install pytest
cp -R docs/examples/hello-roadrunner "$HOME/hello-roadrunner"
cd "$HOME/hello-roadrunner"
git init -b main
roadrunner init .
git add CLAUDE.md README.md tasks .claude hooks
git commit -m "Initialize the Roadrunner hook demo"
roadrunner analyze
roadrunner status
claude
```

Review the project-level settings when Claude asks to trust the project. The generated settings register the hooks while preserving the supplied example roadmap and agent instructions. The Stop hook supplies task briefs and resumes active work. Completion reruns validation and merges each task into `main` before recording it as done.

Inspect `word_counter.py`, `test_word_counter.py`, `logs/trace.jsonl`, and the task work logs. The [example roadmap](tasks/tasks.yaml) defines each task's scope and validation.
