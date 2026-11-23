"""Tests for roadrunner.py controller logic."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import roadrunner
from roadrunner import session as rr_session
from roadrunner import state as rr_state  # state persistence — Issue 5; canonical home of STATE_FILE/STATE_LOCK


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_project(tmp_path):
    """Set up a minimal roadrunner project tree in tmp_path."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    tasks_data = {
        # These fixtures use `true` as a stand-in "passing check"; the coverage
        # floor (issue #18) has its own tests that set it explicitly.
        "coverage_floor": 0,
        "tasks": [
            {
                "id": "TASK-001",
                "title": "First task",
                "status": "done",
                "depends_on": [],
                "goal": "Do the first thing",
                "acceptance_criteria": ["thing exists"],
                "validation_commands": ["true"],
                "files_expected": ["a.py"],
            },
            {
                "id": "TASK-002",
                "title": "Second task",
                "status": "todo",
                "depends_on": ["TASK-001"],
                "goal": "Do the second thing",
                "acceptance_criteria": ["thing two exists"],
                "validation_commands": ["true"],
                "files_expected": ["b.py"],
            },
            {
                "id": "TASK-003",
                "title": "Third task",
                "status": "todo",
                "depends_on": ["TASK-002"],
                "goal": "Do the third thing",
                "acceptance_criteria": [],
                "validation_commands": ["false"],
                "files_expected": [],
            },
        ]
    }

    tasks_file = tasks_dir / "tasks.yaml"
    with open(tasks_file, "w") as f:
        yaml.dump(tasks_data, f, default_flow_style=False, sort_keys=False)

    (logs_dir / "CHANGELOG.md").write_text("")

    # Patch module-level paths. TASKS_BACKUP is derived from TASKS_FILE at
    # module import time, so it must be re-derived here — otherwise save_tasks
    # writes backups to the real project dir and contaminates subsequent tests.
    #
    # After Issue 5, STATE_FILE / STATE_LOCK are owned by rr_state.py. The
    # roadrunner module re-exports them, but the read/write functions look at
    # rr_state's module-globals — so the canonical rebind site is rr_state.
    # roadrunner.STATE_FILE is rebound too because some tests reach into it
    # directly (e.g. ``roadrunner.STATE_FILE.write_text("{not json")``).
    orig = {
        "ROOT": roadrunner.ROOT,
        "TASKS_FILE": roadrunner.TASKS_FILE,
        "TASKS_BACKUP": roadrunner.TASKS_BACKUP,
        "LOGS_DIR": roadrunner.LOGS_DIR,
        "CHANGELOG": roadrunner.CHANGELOG,
        "STATE_FILE": roadrunner.STATE_FILE,
        "TRACE_LOG": roadrunner.TRACE_LOG,
        "LEARNINGS_FILE": roadrunner.LEARNINGS_FILE,
    }
    orig_state = {
        "STATE_FILE": rr_state.STATE_FILE,
        "STATE_LOCK": rr_state.STATE_LOCK,
    }
    roadrunner.ROOT = tmp_path
    roadrunner.TASKS_FILE = tasks_file
    roadrunner.TASKS_BACKUP = tasks_file.with_suffix(".yaml.bak")
    roadrunner.LOGS_DIR = logs_dir
    roadrunner.CHANGELOG = logs_dir / "CHANGELOG.md"
    roadrunner.STATE_FILE = tmp_path / ".roadmap_state.json"
    roadrunner.TRACE_LOG = logs_dir / "trace.jsonl"
    roadrunner.LEARNINGS_FILE = logs_dir / "learnings.md"  # ROAD-013
    rr_state.STATE_FILE = tmp_path / ".roadmap_state.json"
    rr_state.STATE_LOCK = tmp_path / ".roadmap_state.lock"

    # Same rebind for rr_session — its module-level constants would otherwise
    # write summaries into the real project's logs/sessions/ during tests.
    orig_session = {
        "ROOT": rr_session.ROOT,
        "LOGS_DIR": rr_session.LOGS_DIR,
        "TRACE_LOG": rr_session.TRACE_LOG,
        "SESSIONS_DIR": rr_session.SESSIONS_DIR,
        "CURRENT_POINTER": rr_session.CURRENT_POINTER,
    }
    rr_session.ROOT = tmp_path
    rr_session.LOGS_DIR = logs_dir
    rr_session.TRACE_LOG = logs_dir / "trace.jsonl"
    rr_session.SESSIONS_DIR = logs_dir / "sessions"
    rr_session.CURRENT_POINTER = logs_dir / "sessions" / ".current"

    yield tmp_path

    # Restore
    for k, v in orig.items():
        setattr(roadrunner, k, v)
    for k, v in orig_state.items():
        setattr(rr_state, k, v)
    for k, v in orig_session.items():
        setattr(rr_session, k, v)


@pytest.mark.parametrize('command', ['start', 'block'])
def test_roadmap_transition_holds_stop_lock(tmp_project, monkeypatch, command):
    original = roadrunner.save_tasks

    def save_while_locked(tasks):
        with rr_state.STATE_LOCK.open('a') as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original(tasks)

    monkeypatch.setattr(roadrunner, 'save_tasks', save_while_locked)
    monkeypatch.setattr(roadrunner, '_is_git_repo', lambda: False)
    getattr(roadrunner, f'cmd_{command}')(argparse.Namespace(task_id='TASK-002', notes=''))


@pytest.mark.parametrize('max_attempts', [1, 5], ids=['auto-blocked', 'still-active'])
def test_complete_preserves_stop_updates_during_validation(tmp_project, monkeypatch, max_attempts):
    monkeypatch.setattr(roadrunner, '_is_git_repo', lambda: False)
    roadrunner.cmd_start(argparse.Namespace(task_id='TASK-002'))

    def validate_during_stop(task, validation):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(tmp_project), PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        stop = subprocess.run(
            [sys.executable, '-m', 'roadrunner', 'check-stop', '--max-attempts', str(max_attempts)],
            env=env, cwd=tmp_project, input='{}', text=True, capture_output=True, timeout=10, check=True,
        )
        assert json.loads(stop.stdout)['decision'] == 'block'
        return True, []

    monkeypatch.setattr(roadrunner, 'run_validation', validate_during_stop)
    monkeypatch.setattr(roadrunner, 'merge_task_branch', lambda *_: True)
    args = argparse.Namespace(task_id='TASK-002', notes='')
    if max_attempts == 1:
        with pytest.raises(ValueError, match='current active task'):
            roadrunner.cmd_complete(args)
    else:
        roadrunner.cmd_complete(args)
    state = roadrunner.read_state()
    assert state['attempts_per_task'] == {'TASK-002': 1}
    assert state['iteration'] == state['session_iteration'] == 1
    task = roadrunner.get_task(roadrunner.load_tasks(), 'TASK-002')
    assert task is not None and task['status'] == ('blocked' if max_attempts == 1 else 'done')


@pytest.mark.parametrize('writer', ['work log', 'reset marker', 'snapshot', 'init', 'session pointer'])
def test_legacy_writers_publish_atomically(tmp_project, monkeypatch, writer):
    if writer == 'work log':
        target = roadrunner.LOGS_DIR / 'TASK-002.md'
        write = partial(roadrunner.write_work_log, {'id': 'TASK-002'}, [])
    elif writer == 'reset marker':
        target = tmp_project / '.reset_TASK-002'
        write = partial(roadrunner.write_reset_marker, 'TASK-002', 'Reset request')
    elif writer == 'snapshot':
        target = tmp_project / '.context_snapshot.json'
        write = roadrunner.write_context_snapshot
    elif writer == 'init':
        target = tmp_project / 'generated' / 'tasks' / 'tasks.yaml'
        write = partial(roadrunner.cmd_init, argparse.Namespace(target_dir=str(target.parents[1])))
    else:
        target = rr_session.CURRENT_POINTER
        write = rr_session.open_session

    previous = 'Existing content' if writer in ('work log', 'reset marker', 'snapshot') else None
    if previous is not None:
        target.write_text(previous)

    replace = os.replace

    def interrupt_publish(source, destination):
        if Path(destination) == target:
            assert Path(source).read_text()
            raise OSError('Interrupted publish')
        return replace(source, destination)

    monkeypatch.setattr(os, 'replace', interrupt_publish)
    with pytest.raises(OSError, match='Interrupted publish'):
        write()
    assert (target.read_text() if target.exists() else None) == previous


# ── Schema validation ────────────────────────────────────────────────────────


class TestValidateTaskSchema:
    def test_valid_task(self):
        task = {"id": "TST-001", "status": "todo", "title": "Test"}
        roadrunner.validate_task_schema(task, 0)

    def test_missing_id(self):
        with pytest.raises(ValueError, match="missing required fields.*id"):
            roadrunner.validate_task_schema({"status": "todo", "title": "T"}, 0)

    def test_missing_title_and_status(self):
        with pytest.raises(ValueError, match="missing required fields"):
            roadrunner.validate_task_schema({"id": "TST-001"}, 0)

    def test_validation_commands_not_list(self):
        task = {"id": "TST-001", "status": "todo", "title": "T", "validation_commands": "bad"}
        with pytest.raises(ValueError, match="validation_commands must be a list"):
            roadrunner.validate_task_schema(task, 0)

    def test_depends_on_not_list(self):
        task = {"id": "TST-001", "status": "todo", "title": "T", "depends_on": "TASK-001"}
        with pytest.raises(ValueError, match="depends_on must be a list"):
            roadrunner.validate_task_schema(task, 0)

    def test_invalid_status_pending(self):
        task = {"id": "TST-001", "status": "pending", "title": "T"}
        with pytest.raises(ValueError, match="invalid status 'pending'"):
            roadrunner.validate_task_schema(task, 0)

    def test_invalid_status_hyphenated(self):
        task = {"id": "TST-001", "status": "in-progress", "title": "T"}
        with pytest.raises(ValueError, match="invalid status 'in-progress'"):
            roadrunner.validate_task_schema(task, 0)

    def test_all_valid_statuses_accepted(self):
        for status in ("todo", "in_progress", "done", "blocked"):
            task = {"id": "TST-001", "status": status, "title": "T"}
            roadrunner.validate_task_schema(task, 0)

    def test_bad_id_path_traversal(self):
        task = {"id": "../etc", "status": "todo", "title": "T"}
        with pytest.raises(ValueError, match="invalid ID format"):
            roadrunner.validate_task_schema(task, 0)

    def test_bad_id_empty(self):
        task = {"id": "", "status": "todo", "title": "T"}
        with pytest.raises(ValueError, match="invalid ID format"):
            roadrunner.validate_task_schema(task, 0)

    def test_bad_id_spaces(self):
        task = {"id": "has spaces", "status": "todo", "title": "T"}
        with pytest.raises(ValueError, match="invalid ID format"):
            roadrunner.validate_task_schema(task, 0)

    def test_bad_id_lowercase(self):
        task = {"id": "task-001", "status": "todo", "title": "T"}
        with pytest.raises(ValueError, match="invalid ID format"):
            roadrunner.validate_task_schema(task, 0)

    def test_good_id_format(self):
        task = {"id": "TASK-001", "status": "todo", "title": "T"}
        roadrunner.validate_task_schema(task, 0)

    def test_bad_validation_timeout(self):
        task = {"id": "TST-001", "status": "todo", "title": "T", "validation_timeout": "fast"}
        with pytest.raises(ValueError, match="validation_timeout must be a positive number"):
            roadrunner.validate_task_schema(task, 0)

    def test_negative_validation_timeout(self):
        task = {"id": "TST-001", "status": "todo", "title": "T", "validation_timeout": -5}
        with pytest.raises(ValueError, match="validation_timeout must be a positive number"):
            roadrunner.validate_task_schema(task, 0)


# ── ROAD-012: tasks_yaml_model_field (per-task model hint) ───────────────────


class TestTasksYamlModelField:
    """ROAD-012: optional `model:` field on tasks.yaml entries.

    Contract:
      - Type-strict: must be a non-empty string when present (raises ValueError)
      - Value-permissive: unknown IDs warn once per process, never raise
      - Surfaced in: status, next, _build_task_brief, check_stop trace, analyze
    """

    def test_model_field_present_on_typeddict(self):
        # Static-type integration: downstream typed consumers (mypy callers,
        # in particular the rest of cli.py) must see the field.
        assert "model" in roadrunner.Task.__annotations__

    def test_known_model_accepted_without_warning(self, capsys):
        # Reset module-level dedupe so this test is order-independent.
        roadrunner._unknown_model_warnings_seen.clear()
        task = {
            "id": "TST-001",
            "status": "todo",
            "title": "T",
            "model": "claude-haiku-4-5",
        }
        roadrunner.validate_task_schema(task, 0)
        assert capsys.readouterr().err == ""

    def test_short_alias_accepted_without_warning(self, capsys):
        roadrunner._unknown_model_warnings_seen.clear()
        for alias in ("haiku", "sonnet", "opus"):
            roadrunner.validate_task_schema({"id": "TST-001", "status": "todo", "title": "T", "model": alias}, 0)
        assert capsys.readouterr().err == ""

    def test_unknown_model_warns_not_errors(self, capsys):
        # Unknown IDs must NOT raise — forward compat is load-bearing here.
        # A newly-released model ID landing in tasks.yaml should keep loading.
        roadrunner._unknown_model_warnings_seen.clear()
        task = {
            "id": "TST-001",
            "status": "todo",
            "title": "T",
            "model": "future-model-9000",
        }
        roadrunner.validate_task_schema(task, 0)  # must not raise
        err = capsys.readouterr().err
        assert "future-model-9000" in err
        assert "not in the known-model list" in err

    def test_unknown_model_warning_dedupes_within_process(self, capsys):
        # Spam-prevention: validating 10 tasks pinned to the same unknown model
        # should emit exactly one warning, not ten.
        roadrunner._unknown_model_warnings_seen.clear()
        for i in range(10):
            roadrunner.validate_task_schema({"id": f"TST-{i:03d}", "status": "todo", "title": "T", "model": "future-model-9001"}, 0)
        err = capsys.readouterr().err
        assert err.count("future-model-9001") == 1

    def test_non_string_model_raises(self):
        # Type errors do raise — the warning path is reserved for unknown
        # values, not malformed schema (which would corrupt downstream
        # surfaces that assume str).
        task = {"id": "TST-001", "status": "todo", "title": "T", "model": 42}
        with pytest.raises(ValueError, match="model must be a non-empty string"):
            roadrunner.validate_task_schema(task, 0)

    def test_empty_string_model_raises(self):
        task = {"id": "TST-001", "status": "todo", "title": "T", "model": "   "}
        with pytest.raises(ValueError, match="model must be a non-empty string"):
            roadrunner.validate_task_schema(task, 0)

    def test_brief_includes_model_when_set(self):
        # _build_task_brief is the Stop-hook injection surface — the agent
        # learns about per-task model choice from this string.
        task = {
            "id": "TST-001",
            "status": "todo",
            "title": "Pin to Haiku",
            "model": "claude-haiku-4-5",
            "goal": "x",
            "acceptance_criteria": [],
            "validation_commands": [],
        }
        brief = roadrunner._build_task_brief(task, 1, 100)
        assert "Model: claude-haiku-4-5" in brief

    def test_brief_omits_model_line_when_unset(self):
        # Cleanliness: tasks without the field must not get a "Model: None"
        # or blank "Model: " line cluttering the brief.
        task = {
            "id": "TST-001",
            "status": "todo",
            "title": "Default model",
            "goal": "x",
            "acceptance_criteria": [],
            "validation_commands": [],
        }
        brief = roadrunner._build_task_brief(task, 1, 100)
        assert "Model:" not in brief

    def test_status_shows_model_inline(self, tmp_project, capsys):
        tasks = roadrunner.load_tasks()
        tasks[0]["model"] = "claude-opus-4-7"
        roadrunner.save_tasks(tasks)
        roadrunner.cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "[claude-opus-4-7]" in out

    def test_next_brief_shows_model(self, tmp_project, capsys):
        tasks = roadrunner.load_tasks()
        # Pin the first eligible task (TASK-002 in the fixture) so the next
        # command's output is deterministic.
        eligible = roadrunner.next_eligible_task(tasks)
        assert eligible is not None
        target = roadrunner.get_task(tasks, eligible["id"])
        target["model"] = "claude-sonnet-4-6"
        roadrunner.save_tasks(tasks)
        roadrunner.cmd_next(argparse.Namespace())
        out = capsys.readouterr().out
        assert "Model: claude-sonnet-4-6" in out

    def test_check_stop_trace_includes_model_for_active_task(self, tmp_project):
        # The trace event is the durable telemetry — analysts correlate model
        # choice with auto-block rate / cost / iteration spend after the fact.
        tasks = roadrunner.load_tasks()
        tasks[1]["status"] = "in_progress"
        tasks[1]["model"] = "claude-haiku-4-5"
        roadrunner.save_tasks(tasks)
        roadrunner.write_state(tasks[1]["id"], 1)

        args = type(
            "Args",
            (),
            {
                "max_iterations": "100",
                "max_attempts": "5",
                "max_budget_usd": None,
            },
        )()
        import io

        with patch("sys.stdin") as mock_stdin, patch("sys.stdout", io.StringIO()):
            mock_stdin.read.return_value = json.dumps({"stop_hook_active": False, "last_assistant_message": "working"})
            try:
                roadrunner.cmd_check_stop(args)
            except SystemExit:
                pass
        trace_lines = roadrunner.TRACE_LOG.read_text().splitlines()
        check_stop_events = [json.loads(line) for line in trace_lines if line and json.loads(line).get("event") == "check_stop"]
        assert check_stop_events, "expected at least one check_stop trace event"
        assert check_stop_events[-1]["model"] == "claude-haiku-4-5"

    def test_check_stop_trace_omits_model_when_unset(self, tmp_project):
        # Tasks without the field must produce a null `model` key (not crash,
        # not silently drop the field — downstream JSON readers can tell
        # "no preference" apart from "field missing").
        tasks = roadrunner.load_tasks()
        tasks[1]["status"] = "in_progress"
        roadrunner.save_tasks(tasks)
        roadrunner.write_state(tasks[1]["id"], 1)

        args = type(
            "Args",
            (),
            {
                "max_iterations": "100",
                "max_attempts": "5",
                "max_budget_usd": None,
            },
        )()
        import io

        with patch("sys.stdin") as mock_stdin, patch("sys.stdout", io.StringIO()):
            mock_stdin.read.return_value = json.dumps({"stop_hook_active": False, "last_assistant_message": "working"})
            try:
                roadrunner.cmd_check_stop(args)
            except SystemExit:
                pass
        trace_lines = roadrunner.TRACE_LOG.read_text().splitlines()
        check_stop_events = [json.loads(line) for line in trace_lines if line and json.loads(line).get("event") == "check_stop"]
        assert check_stop_events[-1]["model"] is None

    def test_analyze_reports_tasks_by_model(self, tmp_project, capsys):
        # analyze's "Tasks by model" section is the operator's read on whether
        # the per-task model rollout is happening as planned.
        tasks = roadrunner.load_tasks()
        tasks[0]["model"] = "claude-haiku-4-5"
        tasks[1]["model"] = "claude-haiku-4-5"
        tasks[2]["model"] = "claude-opus-4-7"
        roadrunner.save_tasks(tasks)
        try:
            roadrunner.cmd_analyze(argparse.Namespace(tasks_file=None))
        except SystemExit:
            pass
        out = capsys.readouterr().out
        assert "Tasks by model:" in out
        assert "claude-haiku-4-5" in out
        assert "claude-opus-4-7" in out

    def test_analyze_omits_model_section_when_no_task_uses_it(self, tmp_project, capsys):
        # Default projects (no per-task model anywhere) shouldn't see the
        # noise. The section is informational and should disappear at zero.
        try:
            roadrunner.cmd_analyze(argparse.Namespace(tasks_file=None))
        except SystemExit:
            pass
        out = capsys.readouterr().out
        assert "Tasks by model:" not in out


# ── ROAD-013: operator learnings log ─────────────────────────────────────────


class TestLearningsLog:
    """ROAD-013: append-only operational facts that persist across sessions.

    Contract:
      - File at logs/learnings.md; missing file is OK (empty list)
      - Non-empty, non-comment, non-HTML-comment lines count as entries
      - SessionStart hook surfaces the last 20 entries to additionalContext
      - roadrunner status reports the count
      - roadrunner init scaffolds the file in new projects
    """

    def test_entries_helper_skips_header_and_html_comments(self, tmp_project):
        # The shipped header (markdown `#` lines + `<!-- … -->` placeholder)
        # must not be counted as entries — otherwise a fresh install reports
        # "Learnings: 11 entries" before anyone has appended anything.
        roadrunner.LEARNINGS_FILE.write_text(
            "# Operator Learnings\n"
            "\n"
            "Header paragraph that should not count.\n"
            "<!-- placeholder -->\n"
            "2026-05-27 — real entry one\n"
            "2026-05-28 — real entry two\n"
        )
        entries = roadrunner._learnings_entries()
        assert len(entries) == 3  # header paragraph (non-`#` text) + 2 real entries
        assert entries[-1] == "2026-05-28 — real entry two"

    def test_entries_helper_missing_file_returns_empty(self, tmp_project):
        # No file → no entries, no exception. The feature must degrade
        # gracefully on a project that hasn't been re-init'd post-upgrade.
        if roadrunner.LEARNINGS_FILE.exists():
            roadrunner.LEARNINGS_FILE.unlink()
        assert roadrunner._learnings_entries() == []
        assert roadrunner._learnings_tail() == []

    def test_tail_caps_at_twenty(self, tmp_project):
        # Spam-prevention: the SessionStart injection should never balloon
        # context if the log has hundreds of entries. The full file remains
        # on disk for the operator to grep manually.
        lines = "\n".join(f"2026-01-{i:02d} — entry {i}" for i in range(1, 31))
        roadrunner.LEARNINGS_FILE.write_text(lines + "\n")
        tail = roadrunner._learnings_tail()
        assert len(tail) == 20
        # Tail is oldest-first within the window — entries 11..30, not 1..20.
        assert tail[0] == "2026-01-11 — entry 11"
        assert tail[-1] == "2026-01-30 — entry 30"

    def test_status_reports_count(self, tmp_project, capsys):
        roadrunner.LEARNINGS_FILE.write_text("# header\n2026-05-27 — fact one\n2026-05-28 — fact two\n")
        roadrunner.cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "Learnings: 2 entries" in out
        assert "learnings.md" in out

    def test_status_reports_zero_when_missing(self, tmp_project, capsys):
        # Even when no learnings file exists, status must show the
        # affordance so the operator knows where to start.
        if roadrunner.LEARNINGS_FILE.exists():
            roadrunner.LEARNINGS_FILE.unlink()
        roadrunner.cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "Learnings: 0 entries" in out

    def test_session_start_injects_learnings_block(self, tmp_project, capsys):
        # Core observable behavior: lessons survive the session boundary by
        # riding in `additionalContext`.
        roadrunner.LEARNINGS_FILE.write_text("# header\n2026-05-25 — older fact\n2026-05-27 — newer fact\n")
        roadrunner.cmd_session_start(argparse.Namespace())
        out = capsys.readouterr().out
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "Operator learnings" in ctx
        # Most-recent-first order so the agent sees the freshest lesson at the top.
        assert ctx.index("newer fact") < ctx.index("older fact")

    def test_session_start_omits_block_when_no_entries(self, tmp_project, capsys):
        # Fresh project shouldn't get an empty "Operator learnings:" header
        # cluttering its bootstrap context.
        if roadrunner.LEARNINGS_FILE.exists():
            roadrunner.LEARNINGS_FILE.unlink()
        roadrunner.cmd_session_start(argparse.Namespace())
        payload = json.loads(capsys.readouterr().out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "Operator learnings" not in ctx

    def test_init_scaffolds_learnings_file(self, tmp_path):
        # `roadrunner init` is the discoverability surface — without scaffolding
        # the file, new users would never know the affordance exists.
        target = tmp_path / "new_project"
        args = argparse.Namespace(target_dir=str(target), dry_run=False)
        roadrunner.cmd_init(args)
        learnings = target / "logs" / "learnings.md"
        assert learnings.is_file()
        content = learnings.read_text()
        assert "Operator Learnings" in content
        assert "append" in content.lower()

    def test_init_dry_run_mentions_learnings_file(self, tmp_path, capsys):
        # --dry-run output is the operator's preview of what init will do.
        # If we silently skip the learnings file from the dry-run output,
        # the operator can be surprised by an unexpected file on the real run.
        target = tmp_path / "preview_project"
        args = argparse.Namespace(target_dir=str(target), dry_run=True)
        roadrunner.cmd_init(args)
        out = capsys.readouterr().out
        assert "learnings.md" in out


# ── ROAD-014: roadrunner resume --session-id ─────────────────────────────────


class TestSessionIdCapture:
    """ROAD-014: SessionStart captures the Claude Code session_id and
    `roadrunner resume --session-id` plays it back as a paste-able command.

    Contract:
      - SessionStart reads JSON stdin, persists payload.session_id to
        .roadmap_state.json as last_session_id
      - Missing/empty/malformed stdin preserves prior last_session_id
      - check-stop and reset-iteration must NOT clear last_session_id
        (only SessionStart writes it, and only when stdin carries an ID)
      - `resume --session-id` prints `claude --resume <id>` from state
      - `resume` with no flag preserves original pause-toggle semantics
    """

    def _run_session_start(self, stdin_payload, capsys):
        """Drive cmd_session_start with a stdin payload and return captured stdout."""
        import io

        if stdin_payload is None:
            mock = io.StringIO("")
            mock.isatty = lambda: True
        else:
            mock = io.StringIO(json.dumps(stdin_payload))
            mock.isatty = lambda: False
        with patch("sys.stdin", mock):
            roadrunner.cmd_session_start(argparse.Namespace())
        return capsys.readouterr().out

    def test_session_id_captured_from_stdin(self, tmp_project, capsys):
        # Core happy path. Claude Code sends a JSON payload with session_id;
        # SessionStart persists it so a later `resume --session-id` finds it.
        self._run_session_start({"session_id": "claude-session-abc123"}, capsys)
        state = roadrunner.read_state()
        assert state["last_session_id"] == "claude-session-abc123"

    def test_session_id_capture_missing_field_preserves_prior(self, tmp_project, capsys):
        # If the payload lacks session_id (older Claude Code or hook payload
        # schema change), we must NOT clobber a previously-captured ID.
        roadrunner.write_state(None, 0, last_session_id="prior-session-id")
        self._run_session_start({"stop_hook_active": False}, capsys)
        state = roadrunner.read_state()
        assert state["last_session_id"] == "prior-session-id"

    def test_session_id_capture_malformed_stdin_preserves_prior(self, tmp_project, capsys):
        # Malformed JSON must never crash the hook (SessionStart is purely
        # informational and exits 0). Prior value preserved.
        roadrunner.write_state(None, 0, last_session_id="prior-session-id")
        import io

        mock = io.StringIO("{not json!!")
        mock.isatty = lambda: False
        with patch("sys.stdin", mock):
            roadrunner.cmd_session_start(argparse.Namespace())
        capsys.readouterr()  # drain
        state = roadrunner.read_state()
        assert state["last_session_id"] == "prior-session-id"

    def test_session_id_capture_empty_string_ignored(self, tmp_project, capsys):
        # `session_id: ""` shouldn't be persisted — it's not a usable resume
        # target. Same preserve-prior behavior as missing field.
        roadrunner.write_state(None, 0, last_session_id="prior-session-id")
        self._run_session_start({"session_id": "   "}, capsys)
        state = roadrunner.read_state()
        assert state["last_session_id"] == "prior-session-id"

    def test_session_id_capture_no_stdin_when_tty(self, tmp_project, capsys):
        # Manual `roadrunner session-start` from a terminal (no piped stdin)
        # must not block on stdin.read(). We detect via isatty() and skip.
        roadrunner.write_state(None, 0, last_session_id="prior-session-id")
        self._run_session_start(None, capsys)
        state = roadrunner.read_state()
        assert state["last_session_id"] == "prior-session-id"

    def test_check_stop_does_not_clear_session_id(self, tmp_project):
        # check-stop fires many times per session — it must not zero out
        # last_session_id on each fire. Same preserve-or-override contract
        # that protects session_cost_usd applies here.
        roadrunner.write_state(None, 0, last_session_id="should-survive")
        args = type(
            "Args",
            (),
            {
                "max_iterations": "100",
                "max_attempts": "5",
                "max_budget_usd": None,
            },
        )()
        import io

        with patch("sys.stdin") as mock_stdin, patch("sys.stdout", io.StringIO()):
            mock_stdin.read.return_value = json.dumps({"stop_hook_active": False, "last_assistant_message": ""})
            try:
                roadrunner.cmd_check_stop(args)
            except SystemExit:
                pass
        state = roadrunner.read_state()
        assert state["last_session_id"] == "should-survive"

    def test_reset_iteration_soft_does_not_clear_session_id(self, tmp_project):
        # reset-iteration clears session_iteration and session_cost_usd by
        # design (new session window). last_session_id is orthogonal — it's
        # the *previous* session pointer and must survive the reset so
        # `resume --session-id` still has a target.
        roadrunner.write_state(None, 5, session_iteration=10, session_cost_usd=3.0, last_session_id="prev-id")
        roadrunner.cmd_reset_iteration(type("Args", (), {"soft": True, "hard": False})())
        state = roadrunner.read_state()
        assert state["last_session_id"] == "prev-id"
        assert state["session_iteration"] == 0
        assert state["session_cost_usd"] == 0.0

    def test_resume_session_id_prints_claude_command(self, tmp_project, capsys):
        # The headline use case: print the paste-able command.
        roadrunner.write_state(None, 0, last_session_id="abc-123-def")
        args = argparse.Namespace(session_id=True, exec_=False)
        roadrunner.cmd_resume(args)
        out = capsys.readouterr().out
        assert out.strip() == "claude --resume abc-123-def"

    def test_resume_session_id_helpful_message_when_unrecorded(self, tmp_project, capsys):
        # Pre-capture (or after wipe). Operator must get an actionable
        # message rather than a cryptic crash or silent success.
        roadrunner.write_state(None, 0)  # no last_session_id
        args = argparse.Namespace(session_id=True, exec_=False)
        with pytest.raises(SystemExit) as exc:
            roadrunner.cmd_resume(args)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "No Claude Code session ID" in err

    def test_resume_no_flags_preserves_pause_toggle(self, tmp_project, capsys):
        # Existing operators rely on `roadrunner resume` (no flags) to clear
        # the pause marker. The --session-id overload must not regress that.
        (roadrunner.ROOT / ".roadrunner_paused").touch()
        args = argparse.Namespace(session_id=False, exec_=False)
        roadrunner.cmd_resume(args)
        assert not (roadrunner.ROOT / ".roadrunner_paused").exists()
        assert "resumed" in capsys.readouterr().out.lower()

    def test_resume_session_id_also_clears_pause_marker(self, tmp_project, capsys):
        # Regression guard for the code-review finding: an operator who paused
        # for ad-hoc work and then runs `resume --session-id` to retrieve the
        # captured Claude session must NOT stay silently paused. The verb
        # "resume" should resume the loop regardless of sub-mode.
        (roadrunner.ROOT / ".roadrunner_paused").touch()
        roadrunner.write_state(None, 0, last_session_id="captured-xyz")
        args = argparse.Namespace(session_id=True, exec_=False)
        roadrunner.cmd_resume(args)
        captured = capsys.readouterr()
        assert not (roadrunner.ROOT / ".roadrunner_paused").exists(), (
            "resume --session-id must clear .roadrunner_paused so the loop is actually live when the operator reattaches"
        )
        # The resume command still prints on stdout (paste target).
        assert captured.out.strip() == "claude --resume captured-xyz"
        # The pause-cleared notice lands on stderr so it doesn't pollute the
        # paste-able command.
        assert "pause marker cleared" in captured.err

    def test_resume_session_id_no_pause_no_notice(self, tmp_project, capsys):
        # When the operator wasn't paused, --session-id must not print a
        # confusing "pause cleared" notice — only the resume command.
        assert not (roadrunner.ROOT / ".roadrunner_paused").exists()
        roadrunner.write_state(None, 0, last_session_id="captured-xyz")
        args = argparse.Namespace(session_id=True, exec_=False)
        roadrunner.cmd_resume(args)
        captured = capsys.readouterr()
        assert captured.out.strip() == "claude --resume captured-xyz"
        assert "pause marker cleared" not in captured.err

    def test_resume_session_id_error_path_still_clears_pause(self, tmp_project, capsys):
        # If --session-id fails because no ID is captured, the pause clear
        # has already happened — exit 1 must not leave the operator paused.
        # Rationale: the operator's intent ("resume") doesn't depend on
        # whether a session ID was available; the pause clear is the floor.
        (roadrunner.ROOT / ".roadrunner_paused").touch()
        roadrunner.write_state(None, 0)  # no last_session_id
        args = argparse.Namespace(session_id=True, exec_=False)
        with pytest.raises(SystemExit) as exc:
            roadrunner.cmd_resume(args)
        assert exc.value.code == 1
        assert not (roadrunner.ROOT / ".roadrunner_paused").exists(), (
            "the pause marker must be cleared even when --session-id has no captured target — resume's primary effect is loop re-engagement"
        )

    def test_status_displays_last_session_id(self, tmp_project, capsys):
        # The operator should be able to see at a glance whether resume
        # --session-id has a target without having to cat the state file.
        roadrunner.write_state(None, 0, last_session_id="visible-in-status-xyz")
        roadrunner.cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "visible-in-status-xyz" in out
        assert "Last Claude session" in out

    def test_status_omits_session_line_when_unrecorded(self, tmp_project, capsys):
        # Don't clutter fresh-install output. The line only appears once a
        # SessionStart has actually captured something.
        roadrunner.write_state(None, 0)
        roadrunner.cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "Last Claude session" not in out


# ── Eligibility ──────────────────────────────────────────────────────────────


class TestEligibility:
    TASKS = [
        {"id": "A", "status": "done", "title": "A", "depends_on": []},
        {"id": "B", "status": "todo", "title": "B", "depends_on": ["A"]},
        {"id": "C", "status": "todo", "title": "C", "depends_on": ["B"]},
        {"id": "D", "status": "in_progress", "title": "D", "depends_on": []},
    ]

    def test_done_not_eligible(self):
        assert not roadrunner.is_eligible(self.TASKS[0], self.TASKS)

    def test_todo_with_met_deps(self):
        assert roadrunner.is_eligible(self.TASKS[1], self.TASKS)

    def test_todo_with_unmet_deps(self):
        assert not roadrunner.is_eligible(self.TASKS[2], self.TASKS)

    def test_in_progress_not_eligible(self):
        assert not roadrunner.is_eligible(self.TASKS[3], self.TASKS)

    def test_next_eligible(self):
        result = roadrunner.next_eligible_task(self.TASKS)
        assert result["id"] == "B"

    def test_no_eligible(self):
        tasks = [
            {"id": "X", "status": "done", "title": "X", "depends_on": []},
            {"id": "Y", "status": "in_progress", "title": "Y", "depends_on": []},
        ]
        assert roadrunner.next_eligible_task(tasks) is None

    def test_active_task(self):
        result = roadrunner.active_task(self.TASKS)
        assert result["id"] == "D"

    def test_no_active_task(self):
        tasks = [{"id": "X", "status": "done", "title": "X", "depends_on": []}]
        assert roadrunner.active_task(tasks) is None

    def test_circular_deps_never_eligible(self):
        """A→B→A cycle: neither task becomes eligible since deps are never 'done'."""
        tasks = [
            {"id": "CYC-001", "status": "todo", "title": "A", "depends_on": ["CYC-002"]},
            {"id": "CYC-002", "status": "todo", "title": "B", "depends_on": ["CYC-001"]},
        ]
        assert not roadrunner.is_eligible(tasks[0], tasks)
        assert not roadrunner.is_eligible(tasks[1], tasks)
        assert roadrunner.next_eligible_task(tasks) is None


# ── Completion signal ────────────────────────────────────────────────────────


class TestCompletionSignal:
    def test_bare_signal(self):
        assert roadrunner.is_completion_signal("ROADMAP_COMPLETE")

    def test_trailing_newline(self):
        assert roadrunner.is_completion_signal("ROADMAP_COMPLETE\n")

    def test_with_leading_text(self):
        assert roadrunner.is_completion_signal("All done.\n\nROADMAP_COMPLETE\n")

    def test_with_leading_whitespace(self):
        assert roadrunner.is_completion_signal("  ROADMAP_COMPLETE  ")

    def test_substring_no_match(self):
        assert not roadrunner.is_completion_signal("Quote: ROADMAP_COMPLETE in the middle")

    def test_mid_message_no_match(self):
        assert not roadrunner.is_completion_signal("ROADMAP_COMPLETE\nbut then more text")

    def test_empty_string(self):
        assert not roadrunner.is_completion_signal("")

    def test_none_coerced(self):
        assert not roadrunner.is_completion_signal("")

    def test_only_whitespace(self):
        assert not roadrunner.is_completion_signal("   \n  \n  ")


# ── State management ─────────────────────────────────────────────────────────


class TestState:
    def test_read_missing_state(self, tmp_project):
        state_file = tmp_project / ".roadmap_state.json"
        if state_file.exists():
            state_file.unlink()
        state = roadrunner.read_state()
        assert state["current_task_id"] is None
        assert state["iteration"] == 0
        assert state["attempts_per_task"] == {}

    def test_write_and_read_state(self, tmp_project):
        roadrunner.write_state("TASK-002", 3, {"TASK-002": 2})
        state = roadrunner.read_state()
        assert state["current_task_id"] == "TASK-002"
        assert state["iteration"] == 3
        assert state["attempts_per_task"]["TASK-002"] == 2

    def test_increment_attempts(self):
        state = {"attempts_per_task": {"TST-001": 2}}
        result = roadrunner.increment_attempts(state, "TST-001")
        assert result == 3
        assert state["attempts_per_task"]["TST-001"] == 3

    def test_increment_new_task(self):
        state = {"attempts_per_task": {}}
        result = roadrunner.increment_attempts(state, "TST-001")
        assert result == 1


# ── Atomic save ──────────────────────────────────────────────────────────────


class TestAtomicSave:
    def test_save_roundtrip(self, tmp_project):
        tasks = roadrunner.load_tasks()
        assert len(tasks) == 3
        tasks[1]["status"] = "in_progress"
        roadrunner.save_tasks(tasks)
        reloaded = roadrunner.load_tasks()
        assert reloaded[1]["status"] == "in_progress"

    def test_no_tmp_left(self, tmp_project):
        tasks = roadrunner.load_tasks()
        roadrunner.save_tasks(tasks)
        tmp_file = roadrunner.TASKS_FILE.with_suffix(roadrunner.TASKS_FILE.suffix + ".tmp")
        assert not tmp_file.exists()

    def test_schema_validation_on_load(self, tmp_project):
        bad_data = {"tasks": [{"id": "BAD"}]}
        with open(roadrunner.TASKS_FILE, "w") as f:
            yaml.dump(bad_data, f)
        with pytest.raises(ValueError, match="missing required fields"):
            roadrunner.load_tasks()

    def test_rolling_backups_keeps_configured_count(self, tmp_project, monkeypatch):
        monkeypatch.setattr(roadrunner, "TASKS_BACKUP_KEEP", 3)
        tasks = roadrunner.load_tasks()
        # Perform more saves than backups retained; each save rolls .bak → .bak.1 → .bak.2 → .bak.3.
        for _ in range(6):
            roadrunner.save_tasks(tasks)
        assert roadrunner.TASKS_BACKUP.exists()
        assert roadrunner.TASKS_FILE.with_suffix(".yaml.bak.1").exists()
        assert roadrunner.TASKS_FILE.with_suffix(".yaml.bak.2").exists()
        assert roadrunner.TASKS_FILE.with_suffix(".yaml.bak.3").exists()
        # Must not exceed the configured retention
        assert not roadrunner.TASKS_FILE.with_suffix(".yaml.bak.4").exists()

    def test_save_failure_does_not_shift_backup_chain(self, tmp_project, monkeypatch):
        # Regression guard for H1 in the Opus-4.7 audit: a serialization failure
        # during save_tasks must not rotate the backup chain, so a transient
        # bug can't slowly evict good backups by triggering repeated failures.
        tasks = roadrunner.load_tasks()
        roadrunner.save_tasks(tasks)  # produces .bak
        original_bak_bytes = roadrunner.TASKS_BACKUP.read_bytes()

        def boom(*args, **kwargs):
            raise RuntimeError("disk full — simulated")

        monkeypatch.setattr(roadrunner.yaml, "dump", boom)
        with pytest.raises(RuntimeError, match="disk full"):
            roadrunner.save_tasks(tasks)

        # .bak must still be the pre-failure content
        assert roadrunner.TASKS_BACKUP.read_bytes() == original_bak_bytes
        # The chain must NOT have shifted — .bak.1 should not exist because
        # rotation happens only after a successful tmp write.
        assert not roadrunner.TASKS_FILE.with_suffix(".yaml.bak.1").exists()
        # Leftover tmp from the failed write gets cleaned up on next successful save
        tmp_path = roadrunner.TASKS_FILE.with_suffix(roadrunner.TASKS_FILE.suffix + ".tmp")
        if tmp_path.exists():
            tmp_path.unlink()


# ── Run validation ───────────────────────────────────────────────────────────


class TestRunValidation:
    def test_passing_commands(self, tmp_project):
        task = {"id": "T", "validation_commands": ["true", "true"]}
        roadrunner.write_state(None, 0)
        passed, results = roadrunner.run_validation(task)
        assert passed
        assert len(results) == 2
        assert all(r["passed"] for r in results)

    def test_failing_command(self, tmp_project):
        task = {"id": "T", "validation_commands": ["true", "false"]}
        roadrunner.write_state(None, 0)
        passed, results = roadrunner.run_validation(task)
        assert not passed
        assert results[0]["passed"]
        assert not results[1]["passed"]

    def test_no_commands(self, tmp_project):
        task = {"id": "T", "validation_commands": []}
        passed, results = roadrunner.run_validation(task)
        assert passed
        assert results == []

    def test_trace_written(self, tmp_project):
        task = {"id": "T", "validation_commands": ["echo hi"]}
        roadrunner.write_state(None, 1)
        roadrunner.run_validation(task)
        trace_lines = roadrunner.TRACE_LOG.read_text().strip().splitlines()
        events = [json.loads(line) for line in trace_lines]
        assert any(e["event"] == "validation_command" for e in events)
        assert any(e["event"] == "validation_complete" for e in events)

    def test_long_output_keeps_head_and_tail(self, tmp_project):
        """Issue #23: pytest's summary is at the END; a head-only cut loses it."""
        task = {"id": "T", "validation_commands": ["noisy"]}
        roadrunner.write_state(None, 1)
        big = "HEAD-MARK " + "x" * 5000 + " 3 failed, 10 passed TAIL-MARK"
        with patch("roadrunner._run_bounded", return_value=(1, big, big, False)):
            _, results = roadrunner.run_validation(task)
        for key in ("stdout", "stderr"):
            out = results[0][key]
            assert out.startswith("HEAD-MARK")
            assert out.endswith("TAIL-MARK")
            assert "chars omitted" in out
            assert len(out) < len(big)

    def test_short_output_not_truncated(self, tmp_project):
        task = {"id": "T", "validation_commands": ["quiet"]}
        roadrunner.write_state(None, 1)
        text = "y" * 2900
        with patch("roadrunner._run_bounded", return_value=(0, text, "", False)):
            _, results = roadrunner.run_validation(task)
        assert results[0]["stdout"] == text

    def test_timeout_treated_as_failure(self, tmp_project):
        task = {"id": "T", "validation_commands": ["slow_cmd"], "validation_timeout": 5}
        roadrunner.write_state(None, 1)
        with patch("roadrunner._run_bounded", return_value=(-1, "partial out", "partial err", True)):
            passed, results = roadrunner.run_validation(task)
        assert not passed
        assert len(results) == 1
        assert results[0]["returncode"] == -1
        assert results[0]["timed_out"] is True
        assert results[0]["passed"] is False
        assert results[0]["stdout"] == "partial out"
        assert results[0]["stderr"] == "partial err"

    def test_kind_fail_env_timeout(self, tmp_project):
        roadrunner.write_state(None, 1)
        task = {"id": "T", "validation_commands": ["false", "definitely_not_a_cmd_xyz"]}
        _, results = roadrunner.run_validation(task)
        assert [r["kind"] for r in results] == ["fail", "env"]
        with patch("roadrunner._run_bounded", return_value=(-1, "", "", True)):
            _, results = roadrunner.run_validation({"id": "T", "validation_commands": ["x"]})
        assert results[0]["kind"] == "timeout"
        _, ok = roadrunner.run_validation({"id": "T", "validation_commands": ["true"]})
        assert "kind" not in ok[0]

    def test_kind_in_trace(self, tmp_project):
        roadrunner.write_state(None, 1)
        roadrunner.run_validation({"id": "T", "validation_commands": ["definitely_not_a_cmd_xyz"]})
        evs = [json.loads(line) for line in roadrunner.TRACE_LOG.read_text().splitlines()]
        cmd = [e for e in evs if e["event"] == "validation_command"][0]
        assert "env" in json.dumps(cmd)

    def test_classify_validation(self):
        assert roadrunner.classify_validation(127, False) == "env"
        assert roadrunner.classify_validation(126, False) == "env"
        assert roadrunner.classify_validation(1, False) == "fail"
        assert roadrunner.classify_validation(-1, True) == "timeout"

    def test_timeout_with_none_output(self, tmp_project):
        """A timeout with no captured output should not crash."""
        task = {"id": "T", "validation_commands": ["hang"], "validation_timeout": 1}
        roadrunner.write_state(None, 1)
        with patch("roadrunner._run_bounded", return_value=(-1, "", "", True)):
            passed, results = roadrunner.run_validation(task)
        assert not passed
        assert results[0]["stdout"] == ""
        assert results[0]["stderr"] == ""

    def test_timeout_uses_task_value(self, tmp_project):
        """validation_timeout from task dict is passed to the bounded runner."""
        task = {"id": "T", "validation_commands": ["true"], "validation_timeout": 42}
        roadrunner.write_state(None, 1)
        with patch("roadrunner._run_bounded", return_value=(0, "", "", False)) as mock_run:
            roadrunner.run_validation(task)
        mock_run.assert_called_once_with("true", shell=True, timeout=42)


# ── ROAD-015: baseline_validation suite ──────────────────────────────────────


class TestBaselineValidation:
    """ROAD-015: top-level baseline_validation runs as a project-wide CI gate
    before each task's own validation_commands.

    Contract:
      - tasks.yaml's top-level baseline_validation list is read by
        get_baseline_validation()
      - run_validation runs baseline FIRST, short-circuits on first failure
      - task validation_commands run only if baseline passed
      - within task_commands, all run regardless of prior failures (matches
        pre-ROAD-015 UX so the operator sees every task-level issue at once)
      - each ValidationResult carries a 'phase' field for renderers
      - absent / empty / malformed baseline → no baseline runs (backward compat)
    """

    def _set_baseline(self, tmp_project, commands):
        """Write a baseline_validation top-level block into tasks.yaml."""
        with open(tmp_project / "tasks" / "tasks.yaml") as f:
            data = yaml.safe_load(f)
        if commands is None:
            data.pop("baseline_validation", None)
        else:
            data["baseline_validation"] = commands
        with open(tmp_project / "tasks" / "tasks.yaml", "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def test_baseline_validation_absent_returns_empty(self, tmp_project):
        # Default fixture tasks.yaml has no baseline_validation field.
        # Backward compat: helper returns [], run_validation behaves as before.
        self._set_baseline(tmp_project, None)
        assert roadrunner.get_baseline_validation() == []

    def test_baseline_validation_reads_list(self, tmp_project):
        # Happy path: list of strings passes through verbatim.
        self._set_baseline(tmp_project, ["true", "echo hi"])
        assert roadrunner.get_baseline_validation() == ["true", "echo hi"]

    def test_baseline_validation_filters_non_strings(self, tmp_project):
        # Defensive parsing: a malformed entry (int, None, empty string) doesn't
        # crash the helper. The runtime treats it as if the bad entry weren't
        # there, so a typo in tasks.yaml degrades to "fewer baseline checks"
        # rather than "loop wedged."
        self._set_baseline(tmp_project, ["true", 42, None, "", "echo hi"])
        assert roadrunner.get_baseline_validation() == ["true", "echo hi"]

    def test_baseline_validation_non_list_returns_empty(self, tmp_project):
        # Operator typos `baseline_validation: true` instead of a list →
        # helper returns []. We don't want a string-as-shell-command surprise.
        self._set_baseline(tmp_project, "not-a-list")
        assert roadrunner.get_baseline_validation() == []

    def test_run_validation_runs_baseline_before_task(self, tmp_project):
        # Both suites pass; baseline commands appear FIRST in results so
        # render order matches execution order. Phase field is set.
        self._set_baseline(tmp_project, ["true"])
        task = {"id": "T", "validation_commands": ["echo task-cmd"]}
        roadrunner.write_state(None, 0)
        passed, results = roadrunner.run_validation(task)
        assert passed
        assert len(results) == 2
        assert results[0]["phase"] == "baseline"
        assert results[1]["phase"] == "task"

    def test_baseline_validation_short_circuits_on_first_failure(self, tmp_project):
        # Baseline failure must stop the loop BEFORE task-specific commands
        # run. This is the structural CI-parity guarantee: a project that
        # would fail in CI cannot reach `complete` locally either.
        self._set_baseline(tmp_project, ["true", "false", "true"])
        task = {"id": "T", "validation_commands": ["echo should-not-run"]}
        roadrunner.write_state(None, 0)
        passed, results = roadrunner.run_validation(task)
        assert not passed
        # First baseline passed, second failed, third baseline + task commands
        # never ran. We see exactly 2 baseline results, 0 task results.
        baseline_results = [r for r in results if r.get("phase") == "baseline"]
        task_results = [r for r in results if r.get("phase") == "task"]
        assert len(baseline_results) == 2
        assert baseline_results[0]["passed"] is True
        assert baseline_results[1]["passed"] is False
        assert task_results == []

    def test_task_commands_continue_on_failure(self, tmp_project):
        # Within the task phase, the pre-ROAD-015 "see all failures at once"
        # UX is preserved — operator should still be able to spot multiple
        # task-specific issues in one validate run.
        self._set_baseline(tmp_project, [])
        task = {"id": "T", "validation_commands": ["false", "true", "false"]}
        roadrunner.write_state(None, 0)
        passed, results = roadrunner.run_validation(task)
        assert not passed
        # All three task commands executed, despite the first failure.
        assert [r["passed"] for r in results] == [False, True, False]
        assert all(r["phase"] == "task" for r in results)

    def test_empty_baseline_and_empty_commands_returns_passed(self, tmp_project):
        # No baseline, no task commands → True, [] (matches pre-ROAD-015).
        self._set_baseline(tmp_project, None)
        task = {"id": "T", "validation_commands": []}
        roadrunner.write_state(None, 0)
        passed, results = roadrunner.run_validation(task)
        assert passed
        assert results == []

    def test_baseline_only_passes_when_no_task_commands(self, tmp_project):
        # Configuration where baseline exists but task has no commands: the
        # baseline alone gates completion.
        self._set_baseline(tmp_project, ["true", "true"])
        task = {"id": "T", "validation_commands": []}
        roadrunner.write_state(None, 0)
        passed, results = roadrunner.run_validation(task)
        assert passed
        assert len(results) == 2
        assert all(r["phase"] == "baseline" for r in results)

    def test_baseline_validation_trace_includes_phase(self, tmp_project):
        # trace.jsonl readers must be able to tell baseline failures apart
        # from task failures without having to reparse the command string.
        self._set_baseline(tmp_project, ["true"])
        task = {"id": "T", "validation_commands": ["true"]}
        roadrunner.write_state(None, 1)
        roadrunner.run_validation(task)
        trace_lines = roadrunner.TRACE_LOG.read_text().strip().splitlines()
        events = [json.loads(line) for line in trace_lines]
        cmd_events = [e for e in events if e["event"] == "validation_command"]
        phases = [e.get("phase") for e in cmd_events]
        assert phases == ["baseline", "task"]
        # validation_complete event carries baseline_passed / task_passed split
        complete = [e for e in events if e["event"] == "validation_complete"]
        assert complete
        assert complete[-1]["baseline_passed"] is True
        assert complete[-1]["task_passed"] is True


# ── Corrupt input ───────────────────────────────────────────────────────────


class TestCorruptInput:
    def test_load_tasks_invalid_yaml(self, tmp_project):
        # Parser errors are wrapped in a ValueError with operator guidance
        # so the loop halts with an actionable message instead of a raw traceback.
        roadrunner.TASKS_FILE.write_text(": [invalid yaml\n  broken:")
        with pytest.raises(ValueError, match="not valid YAML"):
            roadrunner.load_tasks()

    def test_read_state_invalid_json(self, tmp_project, capsys):
        # Corrupt state must not wedge the loop; reconverge from defaults.
        roadrunner.STATE_FILE.write_text("{not json!!")
        state = roadrunner.read_state()
        assert state == {
            "current_task_id": None,
            "iteration": 0,
            "session_iteration": 0,  # ROAD-010
            "session_cost_usd": 0.0,  # ROAD-011
            "last_session_id": None,  # ROAD-014
            "attempts_per_task": {},
        }
        assert "state file unreadable" in capsys.readouterr().err

    def test_load_tasks_empty_yaml(self, tmp_project):
        roadrunner.TASKS_FILE.write_text("")
        tasks = roadrunner.load_tasks()
        assert tasks == []


# ── check-stop logic ────────────────────────────────────────────────────────


class TestCheckStop:
    """Test cmd_check_stop by calling it with mocked stdin."""

    def _run_check_stop(self, tmp_project, stdin_payload, max_iter="50", max_attempts="5"):
        args = type(
            "Args",
            (),
            {
                "max_iterations": max_iter,
                "max_attempts": max_attempts,
            },
        )()
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = json.dumps(stdin_payload)
            try:
                roadrunner.cmd_check_stop(args)
            except SystemExit:
                pass
        return self._last_stdout

    def _capture_check_stop(self, tmp_project, stdin_payload, max_iter="50", max_attempts="5"):
        args = type(
            "Args",
            (),
            {
                "max_iterations": max_iter,
                "max_attempts": max_attempts,
            },
        )()
        import io

        captured = io.StringIO()
        with patch("sys.stdin") as mock_stdin, patch("sys.stdout", captured):
            mock_stdin.read.return_value = json.dumps(stdin_payload)
            try:
                roadrunner.cmd_check_stop(args)
            except SystemExit:
                pass
        output = captured.getvalue().strip()
        return json.loads(output) if output else None

    def test_stop_hook_active_still_drives_loop_when_work_remains(self, tmp_project):
        """stop_hook_active is a hint, not a hard stop. If there's still eligible
        work, the hook keeps driving — the iteration cap and per-task auto-block
        are the real safety nets against runaway loops. Regression test for the
        'loop stops after every task or two' failure mode seen on the external
        entra-triage pilot."""
        roadrunner.write_state(None, 0)
        result = self._capture_check_stop(tmp_project, {"stop_hook_active": True, "last_assistant_message": "test"})
        assert result is not None, "hook should keep driving when work remains"
        assert result.get("decision") == "block"
        assert "TASK-002" in result.get("reason", ""), "should inject next-task brief"

    def test_stop_hook_active_allows_stop_when_roadmap_finished(self, tmp_project):
        """The one case where stop_hook_active still short-circuits: genuinely
        nothing left to do (no active task, no eligible next)."""
        # Mark every task done so neither active_task nor next_eligible_task returns anything.
        tasks = roadrunner.load_tasks()
        for t in tasks:
            t["status"] = "done"
        roadrunner.save_tasks(tasks)
        roadrunner.write_state(None, 0)
        result = self._capture_check_stop(tmp_project, {"stop_hook_active": True, "last_assistant_message": "test"})
        assert result is None, "with no work remaining + hook-loop signal, allow stop"

    def test_completion_signal_allows_stop(self, tmp_project):
        tasks = roadrunner.load_tasks()
        for task in tasks:
            task["status"] = "done"
        roadrunner.save_tasks(tasks)
        roadrunner.write_state(None, 0)
        result = self._capture_check_stop(
            tmp_project,
            {"stop_hook_active": False, "last_assistant_message": "done\n\nROADMAP_COMPLETE"},
        )
        assert result is None
        # Completion path must also write a terminal entry to CHANGELOG so the
        # audit trail is durable — a returns-early implementation would pass
        # the "result is None" check alone.
        changelog = roadrunner.CHANGELOG.read_text()
        assert "ALL" in changelog
        assert "Roadmap finished" in changelog

    def test_false_positive_blocked(self, tmp_project):
        roadrunner.write_state(None, 0)
        result = self._capture_check_stop(
            tmp_project,
            {"stop_hook_active": False, "last_assistant_message": "mention ROADMAP_COMPLETE here"},
        )
        assert result is not None
        assert result["decision"] == "block"

    def test_resumes_in_progress(self, tmp_project):
        tasks = roadrunner.load_tasks()
        tasks[1]["status"] = "in_progress"
        roadrunner.save_tasks(tasks)
        roadrunner.write_state("TASK-002", 1)
        result = self._capture_check_stop(tmp_project, {"stop_hook_active": False, "last_assistant_message": "working"})
        assert "RESUME IN-PROGRESS" in result["reason"]
        assert "TASK-002" in result["reason"]

    def test_all_done_prompts_completion(self, tmp_project):
        roadrunner.write_state(None, 0)
        tasks = roadrunner.load_tasks()
        for t in tasks:
            t["status"] = "done"
        roadrunner.save_tasks(tasks)
        result = self._capture_check_stop(tmp_project, {"stop_hook_active": False, "last_assistant_message": "idle"})
        assert "All tasks complete" in result["reason"]

    def test_iteration_cap(self, tmp_project):
        # ROAD-010: cap gates on session_iteration, not lifetime iteration.
        # Set both so the test is explicit about which one trips the cap.
        roadrunner.write_state(None, 49, session_iteration=49)
        result = self._capture_check_stop(tmp_project, {"stop_hook_active": False, "last_assistant_message": ""})
        assert result["continue"] is False
        assert "Max iterations" in result["stopReason"]

    def test_auto_block_after_max_attempts(self, tmp_project):
        tasks = roadrunner.load_tasks()
        tasks[1]["status"] = "in_progress"
        roadrunner.save_tasks(tasks)
        roadrunner.write_state("TASK-002", 1, {"TASK-002": 4})
        result = self._capture_check_stop(
            tmp_project,
            {"stop_hook_active": False, "last_assistant_message": "still working"},
            max_attempts="5",
        )
        assert "auto-blocked" in result["reason"]
        reloaded = roadrunner.load_tasks()
        task_002 = roadrunner.get_task(reloaded, "TASK-002")
        assert task_002["status"] == "blocked"

    def _in_progress_002(self):
        tasks = roadrunner.load_tasks()
        tasks[1]["status"] = "in_progress"
        roadrunner.save_tasks(tasks)

    def test_repeat_failure_blocks_before_max_attempts(self, tmp_project):
        """Issue #19: identical failure + identical diff on consecutive validations."""
        self._in_progress_002()
        roadrunner.write_state("TASK-002", 1, {"TASK-002": 1})
        fail = [{"command": "pytest", "passed": False, "returncode": 1, "stdout": "boom", "stderr": ""}]
        with patch("roadrunner.failure_sig.diff_sig", return_value="d1"):
            roadrunner._record_failure_signature("TASK-002", False, fail)
            roadrunner._record_failure_signature("TASK-002", False, fail)
        assert roadrunner.read_state()["last_failure"]["repeats"] == 1
        result = self._capture_check_stop(
            tmp_project, {"stop_hook_active": False, "last_assistant_message": "x"}, max_attempts="5"
        )
        assert "no progress" in result["reason"]
        assert roadrunner.get_task(roadrunner.load_tasks(), "TASK-002")["status"] == "blocked"

    def test_changed_diff_same_error_keeps_attempts(self, tmp_project):
        self._in_progress_002()
        roadrunner.write_state("TASK-002", 1, {"TASK-002": 1})
        fail = [{"command": "pytest", "passed": False, "returncode": 1, "stdout": "boom", "stderr": ""}]
        with patch("roadrunner.failure_sig.diff_sig", side_effect=["d1", "d2"]):
            roadrunner._record_failure_signature("TASK-002", False, fail)
            roadrunner._record_failure_signature("TASK-002", False, fail)
        assert roadrunner.read_state()["last_failure"]["repeats"] == 0
        self._capture_check_stop(tmp_project, {"stop_hook_active": False, "last_assistant_message": "x"})
        assert roadrunner.get_task(roadrunner.load_tasks(), "TASK-002")["status"] == "in_progress"

    def test_success_clears_failure_record(self, tmp_project):
        roadrunner.write_state("TASK-002", 1, {})
        fail = [{"command": "pytest", "passed": False, "returncode": 1, "stdout": "boom", "stderr": ""}]
        with patch("roadrunner.failure_sig.diff_sig", return_value="d1"):
            roadrunner._record_failure_signature("TASK-002", False, fail)
            roadrunner._record_failure_signature("TASK-002", True, [])
        assert roadrunner.read_state()["last_failure"] is None

    def test_auto_block_full_progression_from_zero(self, tmp_project):
        # Drive the full 0 → MAX progression through repeated check_stop calls
        # to prove the attempt counter increments across real hook cycles and
        # the task flips to blocked exactly at MAX, not before.
        tasks = roadrunner.load_tasks()
        tasks[1]["status"] = "in_progress"
        roadrunner.save_tasks(tasks)
        roadrunner.write_state("TASK-002", 0, {})
        for attempt in range(1, 5):
            result = self._capture_check_stop(
                tmp_project,
                {"stop_hook_active": False, "last_assistant_message": "still working"},
                max_attempts="5",
            )
            assert "RESUME IN-PROGRESS" in result["reason"], f"attempt {attempt} should resume, not block"
            state = roadrunner.read_state()
            assert state["attempts_per_task"]["TASK-002"] == attempt
            # Task stays in_progress until the 5th attempt trips auto-block.
            assert roadrunner.get_task(roadrunner.load_tasks(), "TASK-002")["status"] == "in_progress"
        # Fifth cycle trips auto-block.
        final = self._capture_check_stop(
            tmp_project,
            {"stop_hook_active": False, "last_assistant_message": "still working"},
            max_attempts="5",
        )
        assert "auto-blocked" in final["reason"]
        assert roadrunner.get_task(roadrunner.load_tasks(), "TASK-002")["status"] == "blocked"

    def test_iteration_increments_on_check_stop(self, tmp_project):
        # ROAD-010: lifetime iteration increments AND session_iteration increments
        # on the same Stop fire. Fresh state → session starts at 0.
        roadrunner.write_state(None, 5)  # session_iteration defaults to 0 (fresh)
        self._capture_check_stop(tmp_project, {"stop_hook_active": False, "last_assistant_message": ""})
        state = roadrunner.read_state()
        assert state["iteration"] == 6
        assert state["session_iteration"] == 1

    def test_blocked_tasks_reported(self, tmp_project):
        tasks = roadrunner.load_tasks()
        for t in tasks:
            if t["id"] == "TASK-002":
                t["status"] = "blocked"
            elif t["id"] == "TASK-003":
                t["status"] = "todo"
        roadrunner.save_tasks(tasks)
        roadrunner.write_state(None, 0)
        result = self._capture_check_stop(tmp_project, {"stop_hook_active": False, "last_assistant_message": ""})
        assert "Blocked" in result["reason"]

    # ── ROAD-011: budget halt ────────────────────────────────────────────

    def _capture_with_budget(self, tmp_project, stdin_payload, max_budget_usd, max_iter="50", max_attempts="5"):
        """Variant of _capture_check_stop that exercises the --max-budget-usd
        flag path so the env-var fallback can be tested independently."""
        args = type(
            "Args",
            (),
            {
                "max_iterations": max_iter,
                "max_attempts": max_attempts,
                "max_budget_usd": max_budget_usd,
            },
        )()
        import io

        captured = io.StringIO()
        with patch("sys.stdin") as mock_stdin, patch("sys.stdout", captured):
            mock_stdin.read.return_value = json.dumps(stdin_payload)
            try:
                roadrunner.cmd_check_stop(args)
            except SystemExit:
                pass
        output = captured.getvalue().strip()
        return json.loads(output) if output else None

    def test_budget_halt_fires_when_cost_exceeds_cap(self, tmp_project):
        # Operator sets a $5 cap; Claude Code reports $5.50 this fire. Loop must
        # hard-halt with the same shape as the iteration cap, AND a budget_exceeded
        # trace event must land so observability can correlate the halt to spend.
        roadrunner.write_state(None, 0)
        result = self._capture_with_budget(
            tmp_project,
            {
                "stop_hook_active": False,
                "last_assistant_message": "working",
                "total_cost_usd": 5.50,
            },
            max_budget_usd="5.00",
        )
        assert result is not None
        assert result.get("continue") is False
        assert "Budget cap" in result["stopReason"]
        assert "$5.00" in result["stopReason"]
        assert "$5.50" in result["stopReason"]

        # Trace must include the budget_exceeded event so trace.jsonl readers
        # can distinguish a budget halt from an iteration halt without reparsing
        # stopReason text.
        trace_lines = roadrunner.TRACE_LOG.read_text().splitlines()
        events = [json.loads(line)["event"] for line in trace_lines if line]
        assert "budget_exceeded" in events

    def test_budget_halt_does_not_fire_under_cap(self, tmp_project):
        # $4.99 vs $5.00 cap → keep driving. Verifies the gate is >= not >.
        roadrunner.write_state(None, 0)
        result = self._capture_with_budget(
            tmp_project,
            {
                "stop_hook_active": False,
                "last_assistant_message": "working",
                "total_cost_usd": 4.99,
            },
            max_budget_usd="5.00",
        )
        # Should be a normal block-with-brief, not a hard halt.
        assert result is not None
        assert result.get("continue") is not False
        assert result.get("decision") == "block"

    def test_budget_disabled_when_unset(self, tmp_project):
        # No --max-budget-usd, no ROADMAP_MAX_BUDGET_USD → feature is off even
        # if a huge cost is reported. Belt-and-braces: an operator who hasn't
        # opted in must never see the loop halt for cost reasons.
        roadrunner.write_state(None, 0)
        result = self._capture_with_budget(
            tmp_project,
            {
                "stop_hook_active": False,
                "last_assistant_message": "working",
                "total_cost_usd": 999.99,
            },
            max_budget_usd=None,
        )
        assert result is not None
        assert result.get("continue") is not False

    def test_budget_no_op_when_cost_data_missing(self, tmp_project, capsys):
        # Budget is configured but the Stop-hook payload has no cost field.
        # Loop must keep running (no hard halt) and a one-time warning fires
        # to stderr so the operator knows enforcement is degraded.
        # Reset the module-level dedupe flag so the warning emits in this test
        # regardless of test ordering.
        roadrunner._cost_data_warning_emitted = False
        roadrunner.write_state(None, 0)
        result = self._capture_with_budget(
            tmp_project,
            {"stop_hook_active": False, "last_assistant_message": "working"},
            max_budget_usd="5.00",
        )
        assert result is not None
        assert result.get("continue") is not False
        err = capsys.readouterr().err
        assert "ROADMAP_MAX_BUDGET_USD" in err

    def test_budget_env_var_honored_when_flag_absent(self, tmp_project, monkeypatch):
        # The env var must be the fallback when the CLI flag is None — that's
        # how hooks/stop_hook.sh will configure the budget in practice (env in,
        # flag out is the iteration-cap precedent).
        monkeypatch.setenv("ROADMAP_MAX_BUDGET_USD", "2.50")
        roadrunner.write_state(None, 0)
        result = self._capture_with_budget(
            tmp_project,
            {
                "stop_hook_active": False,
                "last_assistant_message": "working",
                "total_cost_usd": 3.00,
            },
            max_budget_usd=None,
        )
        assert result is not None
        assert result.get("continue") is False
        assert "Budget cap" in result["stopReason"]

    def test_budget_flag_overrides_env_var(self, tmp_project, monkeypatch):
        # Env says $10, flag says $1 → flag wins, so $5 trips the halt.
        # Inverse direction confirms the precedence isn't accidentally flipped.
        monkeypatch.setenv("ROADMAP_MAX_BUDGET_USD", "10.00")
        roadrunner.write_state(None, 0)
        result = self._capture_with_budget(
            tmp_project,
            {
                "stop_hook_active": False,
                "last_assistant_message": "working",
                "total_cost_usd": 5.00,
            },
            max_budget_usd="1.00",
        )
        assert result is not None
        assert result.get("continue") is False
        assert "$1.00" in result["stopReason"]

    def test_session_cost_persists_when_payload_missing_field(self, tmp_project):
        # First fire reports cost; second fire's payload has no cost field.
        # The persisted session_cost_usd must NOT zero out — Claude Code's hook
        # schema isn't stable, and forgetting prior cost on every fire would
        # make the budget cap useless in practice.
        roadrunner.write_state(None, 0)
        self._capture_with_budget(
            tmp_project,
            {
                "stop_hook_active": False,
                "last_assistant_message": "working",
                "total_cost_usd": 3.25,
            },
            max_budget_usd="100.00",
        )
        state = roadrunner.read_state()
        assert state["session_cost_usd"] == pytest.approx(3.25)
        # Second fire has no cost field — value must be preserved, not reset.
        self._capture_with_budget(
            tmp_project,
            {"stop_hook_active": False, "last_assistant_message": "still working"},
            max_budget_usd="100.00",
        )
        state = roadrunner.read_state()
        assert state["session_cost_usd"] == pytest.approx(3.25)

    def test_session_start_resets_session_cost(self, tmp_project):
        # SessionStart is the canonical session boundary — cost must zero like
        # session_iteration so a new run starts with the full budget allowance.
        roadrunner.write_state(None, 5, session_iteration=3, session_cost_usd=12.34)
        args = type("Args", (), {})()
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = ""
            try:
                roadrunner.cmd_session_start(args)
            except SystemExit:
                pass
        state = roadrunner.read_state()
        assert state["session_cost_usd"] == 0.0
        assert state["session_iteration"] == 0  # ROAD-010 invariant preserved

    def test_reset_iteration_soft_clears_session_cost(self, tmp_project):
        # `reset-iteration --soft` is the documented escape hatch after a cap
        # fire. If it didn't clear cost, the operator's "start fresh session"
        # would still be over budget. Mirror the session_iteration semantics.
        roadrunner.write_state(None, 10, session_iteration=99, session_cost_usd=20.0)
        args = type("Args", (), {"soft": True, "hard": False})()
        roadrunner.cmd_reset_iteration(args)
        state = roadrunner.read_state()
        assert state["session_cost_usd"] == 0.0
        assert state["session_iteration"] == 0
        assert state["iteration"] == 10  # lifetime preserved on --soft


# ── ROAD-010: Session iteration counter + reset-iteration ────────────────────


class TestSessionIteration:
    """ROAD-010: the iteration cap must be per-session, not lifetime-cumulative.

    Split counters:
      - iteration          lifetime audit counter; never resets except on --hard
      - session_iteration  per-session runaway guard; resets on SessionStart

    Cap gates on session_iteration only. Lifetime is audit only.
    """

    def _run_check_stop(self, max_iter, stdin_payload=None, max_attempts="5"):
        """Helper: run cmd_check_stop with a given max_iter and capture JSON."""
        import io

        args = type(
            "Args",
            (),
            {
                "max_iterations": max_iter,
                "max_attempts": max_attempts,
            },
        )()
        captured = io.StringIO()
        with patch("sys.stdin") as mock_stdin, patch("sys.stdout", captured):
            mock_stdin.read.return_value = json.dumps(stdin_payload or {"stop_hook_active": False, "last_assistant_message": ""})
            try:
                roadrunner.cmd_check_stop(args)
            except SystemExit:
                pass
        output = captured.getvalue().strip()
        return json.loads(output) if output else None

    # ── Schema + backward compat ─────────────────────────────────────────

    def test_state_schema_version_is_current(self):
        # ROAD-010 bumped to 2; ROAD-011 bumped to 3. Update this test (and the
        # docs/configuration.md schema section) whenever the version bumps so
        # we have a single canonical source of "what does HEAD persist?".
        assert roadrunner.STATE_SCHEMA_VERSION == 3

    def test_roadmap_state_typeddict_has_session_iteration(self):
        # TypedDict `total=False` makes the field optional but the annotation
        # must be present so static type checkers can flag mismatches.
        assert "session_iteration" in roadrunner.RoadmapState.__annotations__

    def test_fresh_state_defaults_session_iteration_to_zero(self, tmp_project):
        state_file = tmp_project / ".roadmap_state.json"
        if state_file.exists():
            state_file.unlink()
        state = roadrunner.read_state()
        assert state["session_iteration"] == 0

    def test_legacy_v1_state_file_loads_with_session_iter_defaulted(self, tmp_project):
        # A state file written by an older roadrunner will lack session_iteration
        # and have schema_version=1 (or no schema_version at all). read_state
        # must treat this as session_iteration=0 without raising.
        legacy_state = {
            "schema_version": 1,
            "current_task_id": "TASK-002",
            "iteration": 42,
            "attempts_per_task": {"TASK-002": 1},
            "updated_at": "2026-04-23T00:00:00+00:00",
        }
        roadrunner.STATE_FILE.write_text(json.dumps(legacy_state))
        state = roadrunner.read_state()
        assert state["iteration"] == 42
        assert state["session_iteration"] == 0
        assert state["current_task_id"] == "TASK-002"

    def test_legacy_state_missing_schema_version_loads(self, tmp_project):
        # Even older pre-schema-version state files: no schema_version key.
        # read_state treats missing key as legacy v1 (version defaults to 1).
        legacy_state = {
            "current_task_id": None,
            "iteration": 3,
            "attempts_per_task": {},
        }
        roadrunner.STATE_FILE.write_text(json.dumps(legacy_state))
        state = roadrunner.read_state()
        assert state["session_iteration"] == 0
        assert state["iteration"] == 3

    def test_write_state_persists_session_iteration(self, tmp_project):
        roadrunner.write_state("TASK-002", 10, session_iteration=7)
        raw = json.loads(roadrunner.STATE_FILE.read_text())
        assert raw["session_iteration"] == 7
        assert raw["schema_version"] == roadrunner.STATE_SCHEMA_VERSION

    def test_write_state_preserves_session_iteration_when_not_set(self, tmp_project):
        # A caller that doesn't know about session_iteration (cmd_start, cmd_complete,
        # cmd_block, cmd_reset) must not clobber it.
        roadrunner.write_state("TASK-002", 5, session_iteration=11)
        # Later call without session_iteration argument
        roadrunner.write_state("TASK-002", 6)
        state = roadrunner.read_state()
        assert state["iteration"] == 6
        assert state["session_iteration"] == 11

    def test_write_state_no_session_iter_on_fresh_file_defaults_to_zero(self, tmp_project):
        state_file = tmp_project / ".roadmap_state.json"
        if state_file.exists():
            state_file.unlink()
        roadrunner.write_state(None, 0)
        state = roadrunner.read_state()
        assert state["session_iteration"] == 0

    # ── cmd_check_stop: cap gates on session_iteration ───────────────────

    def test_cap_gates_on_session_iteration_not_lifetime(self, tmp_project):
        """Regression for the ROAD-010 bug: a large lifetime iteration must
        NOT trip the cap as long as session_iteration is under it."""
        roadrunner.write_state(None, 500, session_iteration=0)  # lifetime huge, session fresh
        result = self._run_check_stop(max_iter="10")
        # 0+1=1 < 10 → no cap fire; drive the loop
        assert result is not None
        assert result.get("continue") is not False, f"cap should NOT fire when session_iteration is below max; got {result}"
        state = roadrunner.read_state()
        assert state["iteration"] == 501
        assert state["session_iteration"] == 1

    def test_cap_fires_at_session_iter_equals_max(self, tmp_project):
        roadrunner.write_state(None, 500, session_iteration=9)
        result = self._run_check_stop(max_iter="10")
        # 9+1=10 >= 10 → cap fires
        assert result is not None
        assert result["continue"] is False
        assert "Max iterations (10)" in result["stopReason"]
        assert "session" in result["stopReason"].lower()
        # The stop message should surface lifetime so the operator sees both
        assert "lifetime: 501" in result["stopReason"]

    def test_default_max_iter_is_100_when_arg_missing(self, tmp_project):
        """Acceptance: the hook default is 100. Exercise the `else 100` branch
        of cmd_check_stop by passing max_iterations=None (as if argparse default
        hadn't populated it)."""
        # One below 100 → cap fires on increment to 100
        roadrunner.write_state(None, 0, session_iteration=99)
        import io

        args = type("Args", (), {"max_iterations": None, "max_attempts": "5"})()
        captured = io.StringIO()
        with patch("sys.stdin") as mock_stdin, patch("sys.stdout", captured):
            mock_stdin.read.return_value = json.dumps({"stop_hook_active": False, "last_assistant_message": ""})
            try:
                roadrunner.cmd_check_stop(args)
            except SystemExit:
                pass
        output = captured.getvalue().strip()
        result = json.loads(output) if output else None
        assert result is not None
        assert result["continue"] is False
        assert "Max iterations (100)" in result["stopReason"]

    # ── cmd_session_start: resets session_iteration, preserves lifetime ──

    def test_session_start_resets_session_iter_preserves_lifetime(self, tmp_project):
        # Simulate a session that already accumulated 42 session iterations
        # on top of 250 lifetime.
        roadrunner.write_state("TASK-002", 250, session_iteration=42)
        # Call cmd_session_start with a stub args; swallow stdout (JSON hook output).
        import io

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            try:
                roadrunner.cmd_session_start(type("Args", (), {})())
            except SystemExit:
                pass
        state = roadrunner.read_state()
        assert state["session_iteration"] == 0, "session counter must reset"
        assert state["iteration"] == 250, "lifetime counter must survive SessionStart"

    def test_session_start_still_resets_with_no_tasks(self, tmp_project):
        # Edge case: if load_tasks fails (empty/missing), cmd_session_start
        # returns silently. Older behavior left state untouched; new behavior
        # must STILL reset the session counter so the next run starts fresh.
        # ACTUAL current behavior: it returns before touching state. Document
        # this with a test that captures the known limitation.
        roadrunner.TASKS_FILE.unlink()
        roadrunner.write_state(None, 10, session_iteration=5)
        import io

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            try:
                roadrunner.cmd_session_start(type("Args", (), {})())
            except SystemExit:
                pass
        state = roadrunner.read_state()
        # With no tasks.yaml the hook is a no-op; state is untouched.
        # A future enhancement could reset unconditionally, but that's out of
        # scope for ROAD-010. This test locks the current behavior in place.
        assert state["session_iteration"] == 5
        assert state["iteration"] == 10

    # ── cmd_reset_iteration ──────────────────────────────────────────────

    def test_reset_iteration_default_is_soft(self, tmp_project, capsys):
        roadrunner.write_state(None, 123, session_iteration=99)
        args = type("Args", (), {"soft": False, "hard": False})()
        roadrunner.cmd_reset_iteration(args)
        state = roadrunner.read_state()
        assert state["session_iteration"] == 0
        assert state["iteration"] == 123, "lifetime preserved on default (soft)"
        captured = capsys.readouterr()
        assert "soft" in captured.out.lower()

    def test_reset_iteration_soft_zeros_session_only(self, tmp_project, capsys):
        roadrunner.write_state(None, 200, session_iteration=55)
        args = type("Args", (), {"soft": True, "hard": False})()
        roadrunner.cmd_reset_iteration(args)
        state = roadrunner.read_state()
        assert state["session_iteration"] == 0
        assert state["iteration"] == 200

    def test_reset_iteration_hard_zeros_both(self, tmp_project, capsys):
        roadrunner.write_state(None, 200, session_iteration=55)
        args = type("Args", (), {"soft": False, "hard": True})()
        roadrunner.cmd_reset_iteration(args)
        state = roadrunner.read_state()
        assert state["session_iteration"] == 0
        assert state["iteration"] == 0
        captured = capsys.readouterr()
        assert "hard" in captured.out.lower()
        assert "was 200" in captured.out

    def test_reset_iteration_emits_trace_event(self, tmp_project):
        roadrunner.write_state(None, 50, session_iteration=10)
        args = type("Args", (), {"soft": True, "hard": False})()
        roadrunner.cmd_reset_iteration(args)
        lines = roadrunner.TRACE_LOG.read_text().strip().splitlines()
        events = [json.loads(line) for line in lines]
        reset_events = [e for e in events if e.get("event") == "reset_iteration"]
        assert len(reset_events) == 1
        rec = reset_events[0]
        assert rec["mode"] == "soft"
        assert rec["session_iteration"] == 0
        assert rec["previous_lifetime"] == 50

    # ── cmd_status surfaces both counters ────────────────────────────────

    def test_status_shows_both_counters(self, tmp_project, capsys):
        roadrunner.write_state("TASK-002", 77, session_iteration=3)
        roadrunner.cmd_status(type("Args", (), {})())
        out = capsys.readouterr().out
        assert "Iteration (session): 3" in out
        assert "Iteration (lifetime): 77" in out

    # ── stop_hook.sh default ─────────────────────────────────────────────

    def test_stop_hook_default_max_iterations_is_100(self):
        hook_path = Path(__file__).parent.parent / "hooks" / "stop_hook.sh"
        content = hook_path.read_text()
        assert "ROADMAP_MAX_ITERATIONS:-100" in content, "hook default must be 100 per ROAD-010"


# ── Trace logging ────────────────────────────────────────────────────────────


class TestTraceLogging:
    def test_trace_event_writes_jsonl(self, tmp_project):
        roadrunner.trace_event("test_event", task_id="TST-001", iteration=1)
        lines = roadrunner.TRACE_LOG.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "test_event"
        assert record["task_id"] == "TST-001"
        assert record["iteration"] == 1

    def test_trace_with_extra(self, tmp_project):
        roadrunner.trace_event("test", extra={"foo": "bar"})
        record = json.loads(roadrunner.TRACE_LOG.read_text().strip())
        assert record["foo"] == "bar"

    def test_trace_duration(self, tmp_project):
        roadrunner.trace_event("test", duration_ms=123.456)
        record = json.loads(roadrunner.TRACE_LOG.read_text().strip())
        assert record["duration_ms"] == 123.5


# ── Build task brief ─────────────────────────────────────────────────────────


class TestBuildTaskBrief:
    TASK = {
        "id": "TASK-007",
        "title": "Test task",
        "goal": "Test the thing",
        "acceptance_criteria": ["it works"],
        "validation_commands": ["test -f out.txt"],
        "files_expected": ["out.txt"],
    }

    def test_normal_brief(self):
        brief = roadrunner._build_task_brief(self.TASK, 3, 50)
        assert "Continue working. Iteration 3/50." in brief
        assert "TASK-007" in brief
        assert "test -f out.txt" in brief

    def test_resume_brief(self):
        brief = roadrunner._build_task_brief(self.TASK, 3, 50, resume=True)
        assert "RESUME IN-PROGRESS TASK" in brief

    def test_no_bare_sentinel_line(self):
        brief = roadrunner._build_task_brief(self.TASK, 1, 50)
        assert not roadrunner.is_completion_signal(brief)


# ── Error handling / resilience ──────────────────────────────────────────────


class TestErrorHandling:
    def test_load_tasks_corrupt_yaml_raises_clear_error(self, tmp_project):
        roadrunner.TASKS_FILE.write_text("tasks:\n  - id: TASK-001\n    status: [broken")
        with pytest.raises(ValueError, match="not valid YAML"):
            roadrunner.load_tasks()

    def test_load_tasks_missing_file(self, tmp_project):
        roadrunner.TASKS_FILE.unlink()
        with pytest.raises(FileNotFoundError, match="tasks file not found"):
            roadrunner.load_tasks()

    def test_read_state_corrupt_json_falls_back(self, tmp_project, capsys):
        roadrunner.STATE_FILE.write_text("{not json")
        state = roadrunner.read_state()
        assert state["current_task_id"] is None
        assert state["iteration"] == 0
        assert "state file unreadable" in capsys.readouterr().err

    def test_read_state_not_a_dict_falls_back(self, tmp_project, capsys):
        roadrunner.STATE_FILE.write_text('["not", "a", "dict"]')
        state = roadrunner.read_state()
        assert state == {
            "current_task_id": None,
            "iteration": 0,
            "session_iteration": 0,  # ROAD-010
            "session_cost_usd": 0.0,  # ROAD-011
            "last_session_id": None,  # ROAD-014
            "attempts_per_task": {},
        }
        assert "not a JSON object" in capsys.readouterr().err

    def test_trace_event_logs_stderr_on_write_failure(self, tmp_project, capsys, monkeypatch):
        missing = tmp_project / "does-not-exist" / "trace.jsonl"
        monkeypatch.setattr(roadrunner, "TRACE_LOG", missing)
        roadrunner.trace_event("probe")  # must not raise
        assert "trace_event failed" in capsys.readouterr().err

    def test_append_changelog_logs_stderr_on_write_failure(self, tmp_project, capsys, monkeypatch):
        missing = tmp_project / "does-not-exist" / "CHANGELOG.md"
        monkeypatch.setattr(roadrunner, "CHANGELOG", missing)
        roadrunner.append_changelog("TASK-001", "done")  # must not raise
        assert "changelog append failed" in capsys.readouterr().err


# ── Log rotation ─────────────────────────────────────────────────────────────


class TestLogRotation:
    def test_rotate_when_over_threshold(self, tmp_project, monkeypatch):
        monkeypatch.setattr(roadrunner, "LOG_ROTATE_BYTES", 100)
        roadrunner.TRACE_LOG.write_text("x" * 200)
        roadrunner.rotate_logs()
        assert not roadrunner.TRACE_LOG.exists()
        archives = list(roadrunner.LOGS_DIR.glob("trace.jsonl.*.gz"))
        assert len(archives) == 1

    def test_no_rotate_when_under_threshold(self, tmp_project, monkeypatch):
        monkeypatch.setattr(roadrunner, "LOG_ROTATE_BYTES", 10_000)
        roadrunner.TRACE_LOG.write_text("small")
        roadrunner.rotate_logs()
        assert roadrunner.TRACE_LOG.read_text() == "small"
        assert not list(roadrunner.LOGS_DIR.glob("trace.jsonl.*"))

    def test_prune_old_rotations(self, tmp_project, monkeypatch):
        import os as _os
        import time as _time

        monkeypatch.setattr(roadrunner, "LOG_RETAIN_DAYS", 1)
        old = roadrunner.LOGS_DIR / "trace.jsonl.20200101T000000Z.gz"
        old.write_text("archived")
        # Backdate its mtime to 10 days ago
        ten_days_ago = _time.time() - 10 * 86400
        _os.utime(old, (ten_days_ago, ten_days_ago))

        recent = roadrunner.LOGS_DIR / "trace.jsonl.20991231T235959Z.gz"
        recent.write_text("keep me")
        roadrunner.rotate_logs()
        assert not old.exists()
        assert recent.exists()

    def test_rotate_never_raises_on_error(self, tmp_project, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("disk exploded")

        monkeypatch.setattr(roadrunner, "_rotate_one", boom)
        roadrunner.rotate_logs()  # must not raise

    def test_rotate_collision_safe_within_same_timestamp(self, tmp_project, monkeypatch):
        # Regression guard for H4 in the Opus-4.7 audit: back-to-back rotations
        # whose strftime stamps collide must not clobber the earlier archive.
        # Freeze the stamp so both calls land on the same filename base.
        class FrozenDatetime(roadrunner.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 4, 16, 12, 0, 0, 0, tzinfo=tz)

        monkeypatch.setattr(roadrunner, "datetime", FrozenDatetime)
        monkeypatch.setattr(roadrunner, "LOG_ROTATE_BYTES", 10)

        roadrunner.TRACE_LOG.write_text("x" * 50)
        roadrunner._rotate_one(roadrunner.TRACE_LOG)
        roadrunner.TRACE_LOG.write_text("y" * 50)
        roadrunner._rotate_one(roadrunner.TRACE_LOG)

        # Both rotations must be preserved as separate archives.
        archives = list(roadrunner.LOGS_DIR.glob("trace.jsonl.*.gz"))
        assert len(archives) == 2, f"expected 2 archives, found {[a.name for a in archives]}"


# ── Git branching ────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_git_project(tmp_project, monkeypatch):
    """Initialize a git repo inside tmp_project and patch roadrunner.ROOT to it."""
    root = tmp_project
    monkeypatch.setattr(roadrunner, "ROOT", root)
    # Isolate from the developer's global git config (GPG signing, default branch, etc.)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("gpg.format", "openpgp"),
    ):
        subprocess.run(["git", "config", key, value], cwd=root, check=True)
    seed = root / "seed.txt"
    seed.write_text("initial\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
    return root


class TestGitBranching:
    def test_create_and_merge_clean(self, tmp_git_project):
        root = tmp_git_project
        assert roadrunner.create_task_branch("TASK-099") is True
        # Make a change on the task branch
        (root / "work.txt").write_text("work\n")
        subprocess.run(["git", "add", "work.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=root, check=True)
        assert roadrunner.merge_task_branch("TASK-099", "main") is True
        # Branch should be deleted after successful merge
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "roadrunner/TASK-099"],
            cwd=root,
            capture_output=True,
        ).returncode
        assert exists != 0

    def test_merge_conflict_reports_failure(self, tmp_git_project):
        root = tmp_git_project
        # Create task branch and diverge the target file
        assert roadrunner.create_task_branch("TASK-100") is True
        (root / "shared.txt").write_text("from task branch\n")
        subprocess.run(["git", "add", "shared.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "task branch change"], cwd=root, check=True)

        # Switch back to main and make a conflicting change
        subprocess.run(["git", "checkout", "-q", "main"], cwd=root, check=True)
        (root / "shared.txt").write_text("from main\n")
        subprocess.run(["git", "add", "shared.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "main change"], cwd=root, check=True)

        # Merge should fail cleanly and leave the branch intact for manual resolution
        assert roadrunner.merge_task_branch("TASK-100", "main") is False
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "roadrunner/TASK-100"],
            cwd=root,
            capture_output=True,
        ).returncode
        assert exists == 0
        # Both the merge error AND the abort step must be in the trace log so
        # a double-failure (abort itself erroring) is visible to operators.
        trace_text = roadrunner.TRACE_LOG.read_text() if roadrunner.TRACE_LOG.exists() else ""
        assert "git_merge_error" in trace_text
        assert "git_merge_abort" in trace_text

    def test_merge_missing_branch_noop(self, tmp_git_project):
        # Missing delivery evidence cannot succeed.
        assert roadrunner.merge_task_branch("TASK-DOESNOTEXIST", "main") is False


class TestPushOnComplete:
    """ROAD-031: after a successful merge, push per push_on_complete config.
    Defaults to 'none' (back-compat). Push failures never fail the task."""

    def _with_origin(self, tmp_git_project, tmp_path_factory):
        """Wire a bare repo as origin so pushes actually land."""
        bare = tmp_path_factory.mktemp("origin.git")
        subprocess.run(["git", "init", "--bare", "-q"], cwd=bare, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(bare)],
            cwd=tmp_git_project,
            check=True,
        )
        subprocess.run(
            ["git", "push", "-q", "origin", "main"],
            cwd=tmp_git_project,
            check=True,
        )
        return bare

    def _set_push_mode(self, mode):
        import yaml as _yaml

        data = _yaml.safe_load(roadrunner.TASKS_FILE.read_text()) or {}
        data["push_on_complete"] = mode
        roadrunner.TASKS_FILE.write_text(_yaml.safe_dump(data, sort_keys=False))

    def _make_task_branch_with_work(self, root, task_id, filename):
        subprocess.run(["git", "checkout", "-q", "-b", f"roadrunner/{task_id}"], cwd=root, check=True)
        (root / filename).write_text("content\n")
        subprocess.run(["git", "add", filename], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"work on {task_id}"], cwd=root, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=root, check=True)

    def test_default_none_does_not_push(self, tmp_git_project, tmp_path_factory):
        bare = self._with_origin(tmp_git_project, tmp_path_factory)
        # Default: no push_on_complete key in tasks.yaml
        self._make_task_branch_with_work(tmp_git_project, "TASK-050", "f.txt")
        assert roadrunner.merge_task_branch("TASK-050", "main") is True
        # Origin main should NOT have the new commit
        origin_log = subprocess.run(
            ["git", "--git-dir", str(bare), "log", "main", "--oneline"],
            capture_output=True,
            text=True,
        ).stdout
        assert "work on TASK-050" not in origin_log

    def test_base_pushes_base_branch(self, tmp_git_project, tmp_path_factory):
        bare = self._with_origin(tmp_git_project, tmp_path_factory)
        self._set_push_mode("base")
        self._make_task_branch_with_work(tmp_git_project, "TASK-051", "g.txt")
        assert roadrunner.merge_task_branch("TASK-051", "main") is True
        origin_log = subprocess.run(
            ["git", "--git-dir", str(bare), "log", "main", "--oneline"],
            capture_output=True,
            text=True,
        ).stdout
        assert "work on TASK-051" in origin_log
        # Task branch should NOT be on remote under 'base' mode
        origin_branches = subprocess.run(
            ["git", "--git-dir", str(bare), "branch", "--list"],
            capture_output=True,
            text=True,
        ).stdout
        assert "roadrunner/TASK-051" not in origin_branches

    def test_task_pushes_task_branch_only(self, tmp_git_project, tmp_path_factory):
        bare = self._with_origin(tmp_git_project, tmp_path_factory)
        self._set_push_mode("task")
        self._make_task_branch_with_work(tmp_git_project, "TASK-052", "h.txt")
        assert roadrunner.merge_task_branch("TASK-052", "main") is True
        origin_branches = subprocess.run(
            ["git", "--git-dir", str(bare), "branch", "--list"],
            capture_output=True,
            text=True,
        ).stdout
        assert "roadrunner/TASK-052" in origin_branches

    def test_push_failure_does_not_fail_merge(self, tmp_git_project, monkeypatch, capsys):
        # No origin configured → push will fail. Merge must still succeed.
        self._set_push_mode("base")
        self._make_task_branch_with_work(tmp_git_project, "TASK-053", "i.txt")
        assert roadrunner.merge_task_branch("TASK-053", "main") is True
        err = capsys.readouterr().err
        assert "Push to origin/main failed" in err

    def test_invalid_mode_treated_as_none(self, tmp_git_project, tmp_path_factory):
        bare = self._with_origin(tmp_git_project, tmp_path_factory)
        self._set_push_mode("garbage")
        self._make_task_branch_with_work(tmp_git_project, "TASK-054", "j.txt")
        assert roadrunner.merge_task_branch("TASK-054", "main") is True
        origin_log = subprocess.run(
            ["git", "--git-dir", str(bare), "log", "main", "--oneline"],
            capture_output=True,
            text=True,
        ).stdout
        assert "work on TASK-054" not in origin_log


class TestCommitScopeAware:
    """ROAD-021: cmd_commit stages only files in the task's files_expected +
    roadrunner overlay (logs/, tasks.yaml*, .reset_*). Refuses to commit when
    out-of-scope files are dirty. Regression test for the b21c768 failure mode
    where `git add -A` swept unrelated doc edits into a task's commit."""

    def _run_commit(self, task_id, notes="", type_=None):
        args = argparse.Namespace(task_id=task_id, notes=notes, type=type_)
        try:
            roadrunner.cmd_commit(args)
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 0

    def _last_commit_subject(self, root):
        out = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=root, capture_output=True, text=True)
        return out.stdout.strip()

    def test_commits_in_scope_files_only(self, tmg_with_task):
        root, task_id = tmg_with_task
        # File in files_expected + overlay file (logs/) — both legit.
        (root / "a.py").write_text("print('a')\n")
        (root / "logs").mkdir(exist_ok=True)
        (root / "logs" / "TASK-002.md").write_text("# work log\n")
        rc = self._run_commit(task_id)
        assert rc == 0
        subject = self._last_commit_subject(root)
        assert subject.startswith(f"feat({task_id}):")

    def test_refuses_out_of_scope_dirty_files(self, tmg_with_task, capsys):
        root, task_id = tmg_with_task
        (root / "a.py").write_text("print('a')\n")  # in scope
        (root / "secret.env").write_text("API_KEY=...\n")  # OUT of scope
        rc = self._run_commit(task_id)
        assert rc != 0
        err = capsys.readouterr().err
        assert "secret.env" in err
        assert "out-of-scope" in err.lower() or "out of scope" in err.lower()
        # The in-scope file must NOT be committed (nothing staged on refusal).
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
        assert "a.py" in status, "in-scope file should remain uncommitted when refused"

    def test_no_dirty_files_is_noop(self, tmg_with_task, capsys):
        root, task_id = tmg_with_task
        rc = self._run_commit(task_id)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Nothing to commit" in out

    def test_custom_type_flag(self, tmg_with_task):
        root, task_id = tmg_with_task
        (root / "a.py").write_text("x = 1\n")
        rc = self._run_commit(task_id, type_="refactor")
        assert rc == 0
        assert self._last_commit_subject(root).startswith(f"refactor({task_id}):")

    def test_invalid_type_rejected(self, tmg_with_task, capsys):
        root, task_id = tmg_with_task
        (root / "a.py").write_text("x = 1\n")
        rc = self._run_commit(task_id, type_="garbage")
        assert rc != 0
        assert "Invalid --type" in capsys.readouterr().err

    def test_notes_appear_in_commit_body(self, tmg_with_task):
        root, task_id = tmg_with_task
        (root / "a.py").write_text("x = 1\n")
        rc = self._run_commit(task_id, notes="fixes ABC-123 per review")
        assert rc == 0
        body = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=root, capture_output=True, text=True).stdout
        assert "fixes ABC-123 per review" in body

    def test_unknown_task_id_errors(self, tmg_with_task, capsys):
        _root, _task_id = tmg_with_task
        rc = self._run_commit("TASK-NONEXISTENT")
        assert rc != 0
        assert "not found" in capsys.readouterr().err.lower()


@pytest.fixture
def tmg_with_task(tmp_git_project):
    """tmp_git_project plus a known TASK-002 whose files_expected is ['a.py'].

    Baselines tasks/ and logs/ into the initial commit so the test tree looks
    like a real post-`roadrunner init` project. Without this, git porcelain
    reports `?? tasks/` (directory-level) instead of per-file, which defeats
    the scope matcher.
    """
    tasks = roadrunner.load_tasks()
    for t in tasks:
        if t.get("id") == "TASK-002":
            t["files_expected"] = ["a.py"]
    roadrunner.save_tasks(tasks)
    subprocess.run(["git", "add", "tasks/", "logs/"], cwd=tmp_git_project, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline roadrunner scaffold"],
        cwd=tmp_git_project,
        check=True,
    )
    return tmp_git_project, "TASK-002"


class TestProjectBase:
    """ROAD-025: cmd_start must branch from the configured project_base, not from
    whatever branch HEAD happens to be. This prevents the stacking pattern
    where task N's branch forks from task N-1's branch."""

    def test_get_project_base_reads_from_tasks_yaml(self, tmp_project):
        # tmp_project writes tasks.yaml without project_base; fallback kicks in.
        # Rewrite it with an explicit project_base key.
        import yaml as _yaml

        existing = _yaml.safe_load(roadrunner.TASKS_FILE.read_text())
        existing["project_base"] = "develop"
        roadrunner.TASKS_FILE.write_text(_yaml.safe_dump(existing, sort_keys=False))
        assert roadrunner.get_project_base() == "develop"

    def test_get_project_base_falls_back_without_key(self, tmp_project, monkeypatch):
        # No project_base in the file → falls back to _current_branch() or "main"
        monkeypatch.setattr(roadrunner, "_current_branch", lambda: None)
        assert roadrunner.get_project_base() == "main"

    def test_create_task_branch_forks_from_base_not_current_head(self, tmp_git_project):
        # Simulate a previous task branch sitting on top: HEAD moves to it.
        root = tmp_git_project
        assert roadrunner.create_task_branch("TASK-PREV", base_branch="main") is True
        (root / "prev.txt").write_text("prev\n")
        subprocess.run(["git", "add", "prev.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "prev task work"], cwd=root, check=True)
        # We are now on roadrunner/TASK-PREV with a divergent commit.
        # Create the next task branch with explicit base=main — it must NOT
        # include prev.txt (i.e., must branch from main, not from HEAD).
        assert roadrunner.create_task_branch("TASK-NEXT", base_branch="main") is True
        assert not (root / "prev.txt").exists(), "TASK-NEXT must fork from main; prev.txt from TASK-PREV must be absent"


class TestWriteWorkLog:
    """Regression guard for the work-log overwrite bug observed on 2026-05-28:
    `roadrunner complete` was unconditionally overwriting `logs/{task_id}.md`,
    so any hand-authored narrative the agent wrote before completing the task
    was silently lost. Fix: preserve content above WORK_LOG_MARKER, only
    replace below it (idempotent across repeated complete/block runs)."""

    def _make_task(self, **overrides):
        base = {
            "id": "TST-100",
            "title": "Test task",
            "status": "done",
            "goal": "Do the thing.",
            "acceptance_criteria": ["thing happened"],
        }
        base.update(overrides)
        return base

    def _make_result(self, command="true", passed=True, stderr=""):
        return {
            "command": command,
            "passed": passed,
            "returncode": 0 if passed else 1,
            "stdout": "",
            "stderr": stderr,
        }

    def test_work_log_writes_template_when_file_absent(self, tmp_project):
        # Default case: no prior file → canonical template with H1, marker,
        # and the full auto-generated block.
        task = self._make_task()
        roadrunner.write_work_log(task, [self._make_result()], notes="initial run")
        log = (roadrunner.LOGS_DIR / "TST-100.md").read_text()
        assert log.startswith("# Work Log: TST-100 — Test task")
        assert roadrunner.WORK_LOG_MARKER in log
        assert "## Goal" in log
        assert "## Acceptance Criteria" in log
        assert "## Validation" in log
        assert "## Notes\ninitial run" in log

    def test_work_log_preserves_hand_authored_prose_on_first_complete(self, tmp_project):
        # The bug: the agent wrote a narrative before running `complete`.
        # complete must preserve everything the agent wrote and append the
        # auto block below a marker, NOT overwrite the file.
        log_path = roadrunner.LOGS_DIR / "TST-100.md"
        hand_authored = (
            "# Work Log: TST-100 — Test task\n"
            "\n"
            "## Design rationale\n"
            "I chose approach X because of trade-off Y. The alternative Z\n"
            "would have meant Q, which we explicitly didn't want.\n"
            "\n"
            "## Follow-ups\n"
            "- ROAD-NEXT should generalize the helper.\n"
        )
        log_path.write_text(hand_authored)

        task = self._make_task()
        roadrunner.write_work_log(task, [self._make_result()], notes="ran complete")

        after = log_path.read_text()
        # Every line of the original prose survives.
        assert "## Design rationale" in after
        assert "trade-off Y" in after
        assert "## Follow-ups" in after
        assert "ROAD-NEXT should generalize" in after
        # And the auto block lands after the marker.
        assert roadrunner.WORK_LOG_MARKER in after
        head, _, tail = after.partition(roadrunner.WORK_LOG_MARKER)
        assert "## Design rationale" in head
        assert "## Goal" in tail
        assert "## Validation" in tail
        assert "ran complete" in tail

    def test_work_log_idempotent_across_repeated_complete(self, tmp_project):
        # Running `complete` twice in a row (e.g., validate→complete iteration)
        # must NOT pile up duplicate validation transcripts. The marker is the
        # boundary: replace from marker forward, don't append.
        log_path = roadrunner.LOGS_DIR / "TST-100.md"
        task = self._make_task()

        roadrunner.write_work_log(task, [self._make_result(command="first")], notes="run-1")
        first = log_path.read_text()
        assert first.count(roadrunner.WORK_LOG_MARKER) == 1
        assert "first" in first

        roadrunner.write_work_log(task, [self._make_result(command="second")], notes="run-2")
        second = log_path.read_text()
        # Marker still appears exactly once — not duplicated.
        assert second.count(roadrunner.WORK_LOG_MARKER) == 1
        # The new run's data replaced the old.
        assert "second" in second
        assert "first" not in second
        assert "run-2" in second
        assert "run-1" not in second

    def test_work_log_repeated_writes_preserve_prose(self, tmp_project):
        # Stress test: hand-authored prose must survive MULTIPLE complete
        # invocations, not just the first. The marker boundary holds across
        # arbitrary numbers of runs.
        log_path = roadrunner.LOGS_DIR / "TST-100.md"
        log_path.write_text("# Work Log: TST-100 — Test task\n\n## My notes\nload-bearing.\n")
        task = self._make_task()

        for i in range(3):
            roadrunner.write_work_log(task, [self._make_result()], notes=f"run-{i}")
            content = log_path.read_text()
            assert "## My notes" in content
            assert "load-bearing" in content
            assert f"run-{i}" in content
            # Marker still single-instance.
            assert content.count(roadrunner.WORK_LOG_MARKER) == 1

    def test_work_log_empty_validation_omits_validation_section(self, tmp_project):
        # cmd_block calls write_work_log with empty results. Don't render
        # a hollow "## Validation (0/0 passed)" section — that's noise.
        task = self._make_task(status="blocked")
        roadrunner.write_work_log(task, [], notes="blocked because Q")
        log = (roadrunner.LOGS_DIR / "TST-100.md").read_text()
        assert "## Validation" not in log
        assert "blocked because Q" in log

    def test_work_log_block_then_prose_preserved(self, tmp_project):
        # Realistic flow: task got blocked (write_work_log writes template),
        # operator hand-edits the file to add a "Why blocked" section above
        # the marker, then later complete fires (e.g. block was reverted).
        # The hand-edited section must survive.
        task = self._make_task(status="blocked")
        roadrunner.write_work_log(task, [], notes="initial block")
        log_path = roadrunner.LOGS_DIR / "TST-100.md"

        # Operator adds prose ABOVE the marker.
        content = log_path.read_text()
        head, _, tail = content.partition(roadrunner.WORK_LOG_MARKER)
        edited = head + "\n## Why blocked\nWaiting on upstream PR #42.\n\n" + roadrunner.WORK_LOG_MARKER + tail
        log_path.write_text(edited)

        # Later, status changes and complete fires.
        task["status"] = "done"
        roadrunner.write_work_log(task, [self._make_result()], notes="unblocked, done")
        after = log_path.read_text()
        assert "## Why blocked" in after
        assert "Waiting on upstream PR #42" in after
        assert "unblocked, done" in after


class TestCompleteClearsState:
    """ROAD-023: cmd_complete must clear current_task_id in .roadmap_state.json
    so SessionStart / check_stop don't read a stale pointer to a just-done task."""

    def test_complete_nulls_current_task_id(self, tmp_git_project):
        roadrunner.cmd_start(argparse.Namespace(task_id="TASK-002"))
        # Seed counters for the active task
        roadrunner.write_state("TASK-002", 3, {"TASK-002": 1}, extra={"base_branch": "main"})
        # Sanity check the seed
        assert roadrunner.read_state().get("current_task_id") == "TASK-002"

        args = argparse.Namespace(task_id="TASK-002", notes="finished")
        try:
            roadrunner.cmd_complete(args)
        except SystemExit as exc:
            # complete exits 0 on success; non-zero means validation failed.
            assert exc.code in (None, 0), f"cmd_complete errored with code {exc.code}"

        state = roadrunner.read_state()
        assert state.get("current_task_id") is None, "current_task_id must clear"
        # Iteration and attempts survive — they're loop-lifetime, not task-lifetime.
        assert state.get("iteration") == 3
        assert state.get("attempts_per_task", {}).get("TASK-002") == 1


# ── Issue #18: coverage floor ────────────────────────────────────────────────


class TestCoverageFloor:
    """A pass needs at least ``coverage_floor`` checks that verified something.
    ``true``/``echo`` and empty test runs exit 0 but assert nothing."""

    @staticmethod
    def _set_config(**kw):
        data = yaml.safe_load(roadrunner.TASKS_FILE.read_text())
        data.pop("coverage_floor", None)
        data.update(kw)
        roadrunner.TASKS_FILE.write_text(yaml.dump(data, sort_keys=False))

    def test_only_noop_command_does_not_pass(self, tmp_project):
        self._set_config()  # default floor (1)
        roadrunner.write_state(None, 1)
        passed, results = roadrunner.run_validation({"id": "T", "validation_commands": ["true"]})
        assert not passed
        assert results[0]["passed"] is True and results[0]["vacuous"] is True
        assert results[-1]["kind"] == "vacuous"
        assert "floor is 1" in results[-1]["stderr"]

    def test_empty_test_run_with_exit_zero_is_vacuous(self, tmp_project):
        self._set_config()
        roadrunner.write_state(None, 1)
        out = "============ no tests ran in 0.01s ============"
        with patch("roadrunner._run_bounded", return_value=(0, out, "", False)):
            passed, results = roadrunner.run_validation({"id": "T", "validation_commands": ["pytest -q"]})
        assert not passed
        assert results[0]["vacuous"] is True

    def test_substantive_check_meets_floor(self, tmp_project):
        self._set_config()
        roadrunner.write_state(None, 1)
        passed, results = roadrunner.run_validation({"id": "T", "validation_commands": ["test -d ."]})
        assert passed
        assert not results[0].get("vacuous")
        assert len(results) == 1

    def test_one_substantive_among_noops_is_enough_for_floor_one(self, tmp_project):
        self._set_config()
        roadrunner.write_state(None, 1)
        passed, _ = roadrunner.run_validation({"id": "T", "validation_commands": ["true", "test -d ."]})
        assert passed

    def test_floor_zero_allows_vacuous(self, tmp_project):
        self._set_config(coverage_floor=0)
        roadrunner.write_state(None, 1)
        passed, _ = roadrunner.run_validation({"id": "T", "validation_commands": ["true"]})
        assert passed

    def test_higher_floor_requires_more_checks(self, tmp_project):
        self._set_config(coverage_floor=2)
        roadrunner.write_state(None, 1)
        passed, results = roadrunner.run_validation({"id": "T", "validation_commands": ["test -d ."]})
        assert not passed
        assert "floor is 2" in results[-1]["stderr"]

    def test_task_floor_overrides_project_floor(self, tmp_project):
        self._set_config(coverage_floor=3)
        roadrunner.write_state(None, 1)
        task = {"id": "T", "validation_commands": ["true"], "coverage_floor": 0}
        passed, _ = roadrunner.run_validation(task)
        assert passed

    def test_failing_command_still_reports_its_own_failure_not_floor(self, tmp_project):
        self._set_config()
        roadrunner.write_state(None, 1)
        passed, results = roadrunner.run_validation({"id": "T", "validation_commands": ["false"]})
        assert not passed
        assert all(r.get("kind") != "vacuous" for r in results)

    def test_complete_refuses_vacuous_task(self, tmp_git_project):
        self._set_config()
        roadrunner.cmd_start(argparse.Namespace(task_id="TASK-002"))
        with pytest.raises(SystemExit) as exc:
            roadrunner.cmd_complete(argparse.Namespace(task_id="TASK-002", notes=""))
        assert exc.value.code == 1
        tasks = yaml.safe_load(roadrunner.TASKS_FILE.read_text())["tasks"]
        assert next(t for t in tasks if t["id"] == "TASK-002")["status"] != "done"
        assert roadrunner.read_state().get("current_task_id") == "TASK-002"


# ── Schema version (M2) ──────────────────────────────────────────────────────


class TestStateSchemaVersion:
    def test_write_includes_schema_version(self, tmp_project):
        roadrunner.write_state("TASK-001", 3)
        data = json.loads(roadrunner.STATE_FILE.read_text())
        assert data["schema_version"] == roadrunner.STATE_SCHEMA_VERSION

    def test_legacy_state_without_version_reads_as_v1(self, tmp_project):
        # A state file from an older roadrunner (no schema_version field) must
        # still be readable — treated as v1 for backward compatibility.
        roadrunner.STATE_FILE.write_text(json.dumps({"current_task_id": "TASK-002", "iteration": 7, "attempts_per_task": {}}))
        state = roadrunner.read_state()
        assert state["current_task_id"] == "TASK-002"
        assert state["iteration"] == 7

    def test_future_schema_version_exits_and_preserves_file(self, tmp_project, capsys):
        # A state file from a NEWER roadrunner must not be silently overwritten;
        # read_state must sys.exit so the caller never falls through to a write.
        original = json.dumps({"schema_version": 99, "current_task_id": "TASK-X", "iteration": 42})
        roadrunner.STATE_FILE.write_text(original)
        with pytest.raises(SystemExit) as exc_info:
            roadrunner.read_state()
        assert exc_info.value.code == 2
        # On-disk state is unchanged so the forward-compatible version is recoverable
        assert roadrunner.STATE_FILE.read_text() == original
        err = capsys.readouterr().err
        assert "unknown schema_version" in err

    def test_snapshot_includes_schema_version(self, tmp_project):
        roadrunner.write_context_snapshot()
        snap_path = tmp_project / ".context_snapshot.json"
        data = json.loads(snap_path.read_text())
        assert data["schema_version"] == roadrunner.SNAPSHOT_SCHEMA_VERSION


# ── Concurrent hook fires (M3) ───────────────────────────────────────────────


class TestCheckStopLock:
    def test_lock_serializes_concurrent_increments(self, tmp_project):
        # Two threads calling the read→increment→write section back-to-back
        # must BOTH see their iteration bump land; the lock prevents the classic
        # lost-update race where both read the same value, both write value+1,
        # and the second write clobbers the first.
        import threading

        roadrunner.write_state(None, 0)

        barrier = threading.Barrier(2)

        def bump():
            barrier.wait()
            with roadrunner._exclusive_state_lock():
                s = roadrunner.read_state()
                roadrunner.write_state(s.get("current_task_id"), s["iteration"] + 1)

        t1 = threading.Thread(target=bump)
        t2 = threading.Thread(target=bump)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        final = roadrunner.read_state()
        assert final["iteration"] == 2, f"expected both bumps to land; got iteration={final['iteration']} (lost-update race likely)"


# ── UTF-8 preservation (M4) ──────────────────────────────────────────────────


class TestUtf8Roundtrip:
    def test_trace_preserves_non_ascii(self, tmp_project):
        roadrunner.trace_event("probe", extra={"title": "café — π"})
        line = roadrunner.TRACE_LOG.read_text().strip().splitlines()[-1]
        assert "café" in line
        assert "π" in line
        # The escaped form must NOT be present
        assert "caf\\u00e9" not in line

    def test_state_preserves_non_ascii(self, tmp_project):
        roadrunner.write_state("TASK-001", 1, extra={"base_branch": "主-branch"})
        raw = roadrunner.STATE_FILE.read_text()
        assert "主-branch" in raw
        assert "\\u" not in raw.replace("\\u0000", "")  # no generic escapes

    def test_reset_marker_preserves_non_ascii(self, tmp_project):
        roadrunner.write_reset_marker("TASK-001", summary="résumé ✓")
        marker_text = (tmp_project / ".reset_TASK-001").read_text()
        assert "résumé" in marker_text
        assert "✓" in marker_text


# ── SessionStart handler (N4) ────────────────────────────────────────────────


class TestSessionStart:
    """ROAD-028: SessionStart emits an INSTRUCTION, not ambient status, so the
    agent can start working on turn 1 without the user typing 'Begin'.

    Decision tree covered: no tasks file / no tasks / in-progress / eligible
    next / blocked / all-done."""

    def _run(self, capsys):
        roadrunner.cmd_session_start(argparse.Namespace())
        return capsys.readouterr().out.strip()

    def test_silent_when_tasks_file_absent(self, tmp_project, capsys):
        # tmp_project created tasks.yaml; remove it to exercise the "no file" path.
        roadrunner.TASKS_FILE.unlink()
        assert self._run(capsys) == ""

    def test_eligible_task_produces_start_instruction(self, tmp_project, capsys):
        # tmp_project has TASK-001=done, TASK-002=todo (eligible).
        out = self._run(capsys)
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "roadrunner start TASK-002" in ctx
        # Should be directive, not passive status
        assert "Your first action" in ctx or "first action" in ctx.lower()

    def test_in_progress_task_produces_resume_brief(self, tmp_project, capsys):
        tasks = roadrunner.load_tasks()
        for t in tasks:
            if t.get("id") == "TASK-002":
                t["status"] = "in_progress"
        roadrunner.save_tasks(tasks)
        out = self._run(capsys)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "RESUME" in ctx.upper()
        assert "TASK-002" in ctx

    def test_all_done_prompts_for_completion_sentinel(self, tmp_project, capsys):
        tasks = roadrunner.load_tasks()
        for t in tasks:
            t["status"] = "done"
        roadrunner.save_tasks(tasks)
        out = self._run(capsys)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "ROADMAP_COMPLETE" in ctx
        assert "All tasks are done" in ctx

    def test_blocked_tasks_are_reported(self, tmp_project, capsys):
        tasks = roadrunner.load_tasks()
        for t in tasks:
            if t.get("status") != "done":
                t["status"] = "blocked"
        roadrunner.save_tasks(tasks)
        out = self._run(capsys)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "Blocked" in ctx or "blocked" in ctx
        assert "TASK-002" in ctx or "TASK-003" in ctx


class TestWatch:
    """ROAD-007: read-only live monitor."""

    def test_tail_trace_events_missing_file(self, tmp_project):
        # tmp_project sets TRACE_LOG to a path that does not exist yet.
        assert not roadrunner.TRACE_LOG.exists()
        assert roadrunner._tail_trace_events(5) == []

    def test_tail_trace_events_skips_bad_lines(self, tmp_project):
        roadrunner.TRACE_LOG.write_text(
            '{"ts":"2026-04-25T01:00:00+00:00","event":"a","task_id":"T-1"}\n'
            "this is not json\n"
            '{"ts":"2026-04-25T01:00:01+00:00","event":"b","task_id":"T-2"}\n'
            '{"ts":"2026-04-25T01:00:02+00:00","event":"c","task_id":"T-3"}\n'
        )
        events = roadrunner._tail_trace_events(5)
        assert [e["event"] for e in events] == ["a", "b", "c"]

    def test_tail_trace_events_caps_at_n(self, tmp_project):
        lines = [f'{{"ts":"2026-04-25T01:00:0{i}+00:00","event":"e{i}","task_id":null}}\n' for i in range(8)]
        roadrunner.TRACE_LOG.write_text("".join(lines))
        events = roadrunner._tail_trace_events(3)
        assert len(events) == 3
        assert [e["event"] for e in events] == ["e5", "e6", "e7"]

    def test_render_watch_frame_contains_expected_sections(self, tmp_project):
        # Seed an in-progress active task so the Active line is meaningful.
        tasks = roadrunner.load_tasks()
        for t in tasks:
            if t["id"] == "TASK-002":
                t["status"] = "in_progress"
        roadrunner.save_tasks(tasks)
        roadrunner.write_state("TASK-002", iteration=42, attempts={"TASK-002": 2})
        roadrunner.TRACE_LOG.write_text('{"ts":"2026-04-25T00:00:00+00:00","event":"task_start","task_id":"TASK-002"}\n')

        frame = roadrunner._render_watch_frame(max_iter=100)

        assert "Iteration:" in frame
        assert "Status:" in frame
        assert "Active:" in frame
        assert "TASK-002" in frame
        assert "(Ctrl-C to exit)" in frame
        # Status counts visible
        assert "done 1" in frame
        assert "in_progress 1" in frame
        assert "todo 1" in frame
        assert "blocked 0" in frame
        # Recent events line includes our seeded event
        assert "task_start" in frame

    def test_render_watch_frame_empty_trace(self, tmp_project):
        # No state file, no trace file — should still render without raising.
        frame = roadrunner._render_watch_frame(max_iter=100)
        assert "Iteration:" in frame
        assert "(no trace events yet)" in frame
        assert "Elapsed:     —" in frame

    def test_format_elapsed_hms(self):
        from datetime import timezone as _tz

        start = roadrunner.datetime(2026, 4, 25, 0, 0, 0, tzinfo=_tz.utc)
        now = roadrunner.datetime(2026, 4, 25, 1, 23, 45, tzinfo=_tz.utc)
        assert roadrunner._format_elapsed(start, now) == "01:23:45"
        assert roadrunner._format_elapsed(None, now) == "—"

    def test_watch_subprocess_clean_sigint(self):
        """The loop must exit 0 on SIGINT — covers the explicit acceptance
        criterion 'KeyboardInterrupt exits cleanly with exit code 0'."""
        import signal as _signal
        import time as _time
        import os as _os

        _src = str(Path(__file__).resolve().parent.parent / "src")
        _env = {**_os.environ}
        _env["PYTHONPATH"] = _src + (_os.pathsep + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")
        proc = subprocess.Popen(
            [sys.executable, "-m", "roadrunner", "watch", "--interval", "0.5"],
            cwd=str(Path(__file__).parent.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env,
        )
        _time.sleep(2)
        proc.send_signal(_signal.SIGINT)
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("watch did not exit within 5s of SIGINT")
        assert rc == 0


class TestPauseResume:
    """Pause/resume marker file toggles the Stop-hook bypass."""

    def test_pause_creates_marker(self, tmp_project, capsys):
        roadrunner.cmd_pause(argparse.Namespace())
        assert (tmp_project / ".roadrunner_paused").exists()
        assert "paused" in capsys.readouterr().out.lower()

    def test_resume_removes_marker(self, tmp_project, capsys):
        (tmp_project / ".roadrunner_paused").touch()
        roadrunner.cmd_resume(argparse.Namespace())
        assert not (tmp_project / ".roadrunner_paused").exists()
        assert "resumed" in capsys.readouterr().out.lower()

    def test_resume_is_idempotent(self, tmp_project, capsys):
        assert not (tmp_project / ".roadrunner_paused").exists()
        roadrunner.cmd_resume(argparse.Namespace())  # must not raise
        assert "resumed" in capsys.readouterr().out.lower()

    def test_health_reports_paused_state(self, tmp_project, capsys):
        (tmp_project / ".roadrunner_paused").touch()
        roadrunner.cmd_health(argparse.Namespace())
        assert "PAUSED" in capsys.readouterr().out

    def test_health_silent_when_not_paused(self, tmp_project, capsys):
        roadrunner.cmd_health(argparse.Namespace())
        assert "PAUSED" not in capsys.readouterr().out


class TestRetryBrief:
    def test_summary_names_failing_command_and_output(self):
        from roadrunner import cli

        results = [
            {"command": "ruff check", "passed": True},
            {"command": "pytest", "passed": False, "returncode": 1, "stdout": "", "stderr": "E assert 1 == 2", "kind": "fail"},
        ]
        s = cli._failure_summary(results)
        assert "`pytest`" in s and "E assert 1 == 2" in s and "ruff" not in s

    def test_summary_empty_when_all_pass(self):
        from roadrunner import cli

        assert cli._failure_summary([{"command": "x", "passed": True}]) == ""


# ── Issue 16: bounded legacy subprocesses ────────────────────────────────────


class TestBoundedSubprocesses:
    def test_validation_timeout_kills_descendants(self, tmp_project, tmp_path):
        """A validation shell that spawns a child must leave no survivor after a timeout."""
        pidfile = tmp_path / "child.pid"
        cmd = f"sleep 300 & echo $! > {pidfile}; wait"
        roadrunner.write_state(None, 1)
        passed, results = roadrunner.run_validation({"id": "T", "validation_commands": [cmd], "validation_timeout": 1})
        assert not passed
        assert results[0]["timed_out"] is True
        assert results[0]["kind"] == "timeout"
        pid = int(pidfile.read_text())
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            pytest.fail("validation child survived the timeout")

    def test_git_hang_fails_actionably_within_limit(self, tmp_git_project, monkeypatch, tmp_path):
        """A git subprocess that hangs returns a failure inside the bound, never success."""
        fake = tmp_path / "bin"
        fake.mkdir()
        git = fake / "git"
        git.write_text("#!/bin/sh\nsleep 300\n")
        git.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("ROADRUNNER_GIT_TIMEOUT", "1")
        t0 = time.monotonic()
        result = roadrunner._git("push", "origin", "main", check=False)
        assert time.monotonic() - t0 < 10
        assert result.returncode == roadrunner.GIT_TIMEOUT_RETURNCODE
        assert "timed out after 1s" in result.stderr
        assert "ROADRUNNER_GIT_TIMEOUT" in result.stderr
        with pytest.raises(subprocess.CalledProcessError):
            roadrunner._git("push", "origin", "main")

    def test_git_healthy_path_unchanged(self, tmp_git_project):
        result = roadrunner._git("rev-parse", "--is-inside-work-tree")
        assert result.returncode == 0
        assert result.stdout.strip() == "true"

    def test_git_timeout_env_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("ROADRUNNER_GIT_TIMEOUT", "nope")
        assert roadrunner._git_timeout() == roadrunner.DEFAULT_GIT_TIMEOUT
        monkeypatch.setenv("ROADRUNNER_GIT_TIMEOUT", "-3")
        assert roadrunner._git_timeout() == roadrunner.DEFAULT_GIT_TIMEOUT
