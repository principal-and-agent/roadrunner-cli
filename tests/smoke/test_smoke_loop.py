"""Smoke loop: simulate a SessionStart -> Stop cycle without invoking Claude.

Drives ``roadrunner.cmd_check_stop`` and ``roadrunner.cmd_session_start``
directly with mocked stdin to exercise the cross-session state machine.
The class of regression this is here to catch is the kind that does not
manifest within a single pytest run -- specifically, the one Issue 1 of
the 2026-04-24 audit flagged: the per-session iteration counter must reset
on SessionStart, and the cap must gate on ``session_iteration``, not on the
lifetime ``iteration``.

Tests that depend on the ROAD-010 (per-session iteration counter) feature
are gated behind ``_road010_present()`` and skip cleanly when that work has
not yet landed on the trunk. Once ROAD-010 merges to main, the gated tests
activate automatically and start guarding the regression.

Runs entirely in tmp_path with the toy roadmap copied from
``tests/smoke/toy-roadmap/``. Target wall time: < 10 seconds on CI.
"""

import inspect
import io
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

import roadrunner
from roadrunner import session as rr_session
from roadrunner import state as rr_state

_TOY_ROADMAP = Path(__file__).parent / "toy-roadmap"


def _road010_present() -> bool:
    """True iff the per-session iteration counter (ROAD-010) has landed.

    Behavior/schema-based probe rather than a signature check.  ROAD-010
    may surface ``session_iteration`` through a new ``write_state`` kwarg,
    through the existing ``extra`` payload, or via internal state mutation
    inside ``cmd_check_stop`` — a signature-only check would miss the
    latter two and these tests would stay skipped forever, defeating the
    weekly regression guard.

    We check two signals, either of which is sufficient:
      1. ``RoadmapState.__annotations__`` declares ``session_iteration``.
         This is the contract the persisted state surface honours.
      2. ``write_state`` accepts a ``session_iteration`` kwarg.  Two of
         the gated tests use this kwarg to seed state, so its presence is
         the actual prerequisite for those tests to run.
    """
    annotations = getattr(roadrunner.RoadmapState, "__annotations__", {})  # type: ignore[attr-defined]
    if "session_iteration" in annotations:
        return True
    try:
        sig = inspect.signature(roadrunner.write_state)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        return False
    return "session_iteration" in sig.parameters


# ── Fixture ─────────────────────────────────────────────────────────────────


@pytest.fixture
def smoke_project(tmp_path):
    """Materialise the toy roadmap into tmp_path and rebind module-level paths.

    Mirrors the setup of ``tests/test_roadrunner.py::tmp_project`` but copies
    the persistent toy roadmap fixture instead of generating one inline. This
    way the smoke loop exercises the same data shape a real project would.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    shutil.copy(_TOY_ROADMAP / "tasks.yaml", tasks_dir / "tasks.yaml")
    (logs_dir / "CHANGELOG.md").write_text("")

    orig = {
        "ROOT": roadrunner.ROOT,
        "TASKS_FILE": roadrunner.TASKS_FILE,
        "TASKS_BACKUP": roadrunner.TASKS_BACKUP,
        "LOGS_DIR": roadrunner.LOGS_DIR,
        "CHANGELOG": roadrunner.CHANGELOG,
        "STATE_FILE": roadrunner.STATE_FILE,
        "TRACE_LOG": roadrunner.TRACE_LOG,
    }
    orig_state = {
        "STATE_FILE": rr_state.STATE_FILE,
        "STATE_LOCK": rr_state.STATE_LOCK,
    }
    orig_session = {
        "ROOT": rr_session.ROOT,
        "LOGS_DIR": rr_session.LOGS_DIR,
        "TRACE_LOG": rr_session.TRACE_LOG,
        "SESSIONS_DIR": rr_session.SESSIONS_DIR,
        "CURRENT_POINTER": rr_session.CURRENT_POINTER,
    }
    roadrunner.ROOT = tmp_path
    roadrunner.TASKS_FILE = tasks_dir / "tasks.yaml"
    roadrunner.TASKS_BACKUP = (tasks_dir / "tasks.yaml").with_suffix(".yaml.bak")
    roadrunner.LOGS_DIR = logs_dir
    roadrunner.CHANGELOG = logs_dir / "CHANGELOG.md"
    roadrunner.STATE_FILE = tmp_path / ".roadmap_state.json"
    roadrunner.TRACE_LOG = logs_dir / "trace.jsonl"
    # State and session I/O live in their own modules after Issues 5/6;
    # rebinding only `roadrunner.*` would leak writes to the real project.
    rr_state.STATE_FILE = tmp_path / ".roadmap_state.json"
    rr_state.STATE_LOCK = tmp_path / ".roadmap_state.lock"
    rr_session.ROOT = tmp_path
    rr_session.LOGS_DIR = logs_dir
    rr_session.TRACE_LOG = logs_dir / "trace.jsonl"
    rr_session.SESSIONS_DIR = logs_dir / "sessions"
    rr_session.CURRENT_POINTER = logs_dir / "sessions" / ".current"

    yield tmp_path

    for k, v in orig.items():
        setattr(roadrunner, k, v)
    for k, v in orig_state.items():
        setattr(rr_state, k, v)
    for k, v in orig_session.items():
        setattr(rr_session, k, v)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _drive_check_stop(stdin_payload, *, max_iter="100", max_attempts="5"):
    """Invoke cmd_check_stop with mocked stdin; capture decoded stdout."""
    args = type(
        "Args",
        (),
        {"max_iterations": max_iter, "max_attempts": max_attempts},
    )()
    captured = io.StringIO()
    with patch("sys.stdin") as mock_stdin, patch("sys.stdout", captured):
        mock_stdin.read.return_value = json.dumps(stdin_payload)
        try:
            roadrunner.cmd_check_stop(args)
        except SystemExit:
            pass
    out = captured.getvalue().strip()
    return json.loads(out) if out else None


def _drive_session_start():
    """Invoke cmd_session_start; the side effect we care about is the state reset."""
    args = type("Args", (), {})()
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        try:
            roadrunner.cmd_session_start(args)
        except SystemExit:
            pass
    out = captured.getvalue().strip()
    return json.loads(out) if out else None


# ── Tests ───────────────────────────────────────────────────────────────────


class TestSmokeLoopStateMachine:
    """Behaviours that can only fail across simulated session boundaries."""

    def test_lifetime_iteration_advances_each_check_stop(self, smoke_project):
        """Baseline that works on any state schema: lifetime iteration ticks
        on every Stop fire. Guards against a regression that drops the
        increment entirely."""
        roadrunner.write_state(None, 0)
        for _ in range(3):
            _drive_check_stop({"stop_hook_active": False, "last_assistant_message": ""})
        s = roadrunner.read_state()
        assert s["iteration"] == 3, "lifetime iteration must advance with each Stop"

    @pytest.mark.skipif(
        not _road010_present(),
        reason="requires ROAD-010 (per-session iteration counter)",
    )
    def test_iteration_counter_resets_on_session_start(self, smoke_project):
        """The Issue 1 regression. Lifetime keeps climbing; session resets to 0."""
        for _ in range(3):
            _drive_check_stop({"stop_hook_active": False, "last_assistant_message": ""})
        s1 = roadrunner.read_state()
        assert s1["session_iteration"] == 3, "session counter should advance with each Stop"
        assert s1["iteration"] == 3, "lifetime counter should advance in lockstep at this point"

        _drive_session_start()
        s_reset = roadrunner.read_state()
        assert s_reset["session_iteration"] == 0, "SessionStart MUST reset session counter"
        assert s_reset["iteration"] == 3, "SessionStart MUST preserve lifetime counter"

        for _ in range(2):
            _drive_check_stop({"stop_hook_active": False, "last_assistant_message": ""})
        s2 = roadrunner.read_state()
        assert s2["session_iteration"] == 2, "session counter should restart from zero"
        assert s2["iteration"] == 5, "lifetime counter should keep climbing across sessions"

    @pytest.mark.skipif(
        not _road010_present(),
        reason="requires ROAD-010 (per-session iteration counter)",
    )
    def test_cap_fires_on_session_iteration_not_lifetime(self, smoke_project):
        """Cap is a runaway-protection primitive. It MUST gate on session_iteration.

        Pre-loads a high lifetime with zero session. If the cap mistakenly read
        lifetime, the very first Stop would halt the loop on entry."""
        roadrunner.write_state(None, 999, session_iteration=0)
        result = _drive_check_stop(
            {"stop_hook_active": False, "last_assistant_message": ""},
            max_iter="3",
        )
        assert result is not None, "lifetime above any cap should NOT short-circuit"
        assert result.get("decision") == "block", (
            "with session_iteration=1 and cap=3, the hook should keep driving"
        )

    @pytest.mark.skipif(
        not _road010_present(),
        reason="requires ROAD-010 (per-session iteration counter)",
    )
    def test_cap_halts_session_when_session_iteration_exceeds(self, smoke_project):
        """Conversely: when session_iteration crosses the cap, halt cleanly."""
        roadrunner.write_state(None, 50, session_iteration=2)
        result = _drive_check_stop(
            {"stop_hook_active": False, "last_assistant_message": ""},
            max_iter="3",
        )
        assert result is not None, "halt path should emit a stopReason payload"
        assert result.get("continue") is False, "cap hit must halt the loop"
        assert "Max iterations" in result.get("stopReason", "")

    def test_complete_clears_current_task_id(self, smoke_project):
        """After cmd_complete fires, current_task_id must be None so SessionStart
        does not emit stale resume briefs (the ROAD-023 regression). Works on
        any state schema; does NOT depend on ROAD-010."""
        # Patch _git so cmd_complete's branch-merge path no-ops cleanly inside tmp_path.
        with patch.object(
            roadrunner,
            "_git",
            return_value=type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        ):
            args = type(
                "Args",
                (),
                {"task_id": "SMOKE-001", "notes": "smoke complete"},
            )()
            roadrunner.cmd_start(args)
            roadrunner.write_state("SMOKE-001", 1, {"SMOKE-001": 1})
            roadrunner.cmd_complete(args)

        s = roadrunner.read_state()
        assert s.get("current_task_id") is None, (
            "cmd_complete MUST clear current_task_id"
        )
        assert s.get("iteration") == 1, "lifetime iteration must survive complete"
        assert s.get("attempts_per_task", {}).get("SMOKE-001") == 1, (
            "attempts_per_task must survive complete"
        )

    def test_roadmap_complete_signal_exits_cleanly(self, smoke_project):
        """The ROADMAP_COMPLETE sentinel halts the loop and writes a terminal
        CHANGELOG entry. A returns-early-without-logging implementation would
        silently regress the audit trail."""
        tasks = roadrunner.load_tasks()
        for task in tasks:
            task["status"] = "done"
        roadrunner.save_tasks(tasks)
        roadrunner.write_state(None, 0)
        result = _drive_check_stop(
            {
                "stop_hook_active": False,
                "last_assistant_message": "all done\n\nROADMAP_COMPLETE",
            }
        )
        assert result is None, "ROADMAP_COMPLETE must allow the stop (no JSON emitted)"
        log = roadrunner.CHANGELOG.read_text()
        assert "ALL" in log and "complete" in log, (
            "completion path must append a terminal CHANGELOG entry"
        )
