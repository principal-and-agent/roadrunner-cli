"""Tests for rr_session — session summary observability primitive (Issue 6)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from roadrunner import session as rr_session


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_session_root(tmp_path, monkeypatch):
    """Re-bind module-level paths to tmp_path so tests don't touch the live
    logs/sessions directory. Mirrors the rebind pattern used by tmp_project
    in test_roadrunner.py.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(rr_session, "ROOT", tmp_path)
    monkeypatch.setattr(rr_session, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(rr_session, "TRACE_LOG", logs_dir / "trace.jsonl")
    monkeypatch.setattr(rr_session, "SESSIONS_DIR", logs_dir / "sessions")
    monkeypatch.setattr(rr_session, "CURRENT_POINTER", logs_dir / "sessions" / ".current")
    return tmp_path


def _write_trace(tmp_path: Path, events: list[dict]) -> None:
    trace = tmp_path / "logs" / "trace.jsonl"
    with open(trace, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _ts(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


# ── open_session ─────────────────────────────────────────────────────────────


class TestOpenSession:
    def test_creates_shell_and_pointer(self, tmp_session_root):
        path = rr_session.open_session()
        assert path.exists()
        assert rr_session.CURRENT_POINTER.exists()
        assert rr_session.CURRENT_POINTER.read_text().strip() == str(path)

        data = json.loads(path.read_text())
        assert "session_id" in data
        assert data["ended_at"] is None
        assert data["tasks_completed"] == []
        assert data["iterations"] == 0

    def test_finalizes_prior_open_session(self, tmp_session_root):
        first = rr_session.open_session()
        first_data = json.loads(first.read_text())
        assert first_data["ended_at"] is None

        # Second open should finalize the first.
        second = rr_session.open_session()
        assert second != first
        first_after = json.loads(first.read_text())
        assert first_after["ended_at"] is not None

        # Pointer now refers to the second.
        assert rr_session.CURRENT_POINTER.read_text().strip() == str(second)


# ── finalize_current ─────────────────────────────────────────────────────────


class TestFinalizeCurrent:
    def test_no_open_session_is_noop(self, tmp_session_root):
        assert rr_session.finalize_current() is None

    def test_rolls_up_trace_events(self, tmp_session_root, tmp_path):
        path = rr_session.open_session()
        # Trace events written AFTER session start should be aggregated.
        events = [
            {"ts": _ts(1), "event": "task_start", "task_id": "T-1"},
            {"ts": _ts(2), "event": "validate_end", "task_id": "T-1", "passed": True},
            {"ts": _ts(3), "event": "task_complete", "task_id": "T-1"},
            {"ts": _ts(4), "event": "task_start", "task_id": "T-2"},
            {"ts": _ts(5), "event": "validate_end", "task_id": "T-2", "passed": False},
            {"ts": _ts(6), "event": "validate_end", "task_id": "T-2", "passed": True},
            {"ts": _ts(7), "event": "task_complete", "task_id": "T-2"},
            {"ts": _ts(8), "event": "check_stop", "iteration": 14},
            {"ts": _ts(9), "event": "task_block", "task_id": "T-3"},
        ]
        _write_trace(tmp_path, events)
        rr_session.finalize_current()
        data = json.loads(path.read_text())

        assert data["ended_at"] is not None
        assert data["tasks_completed"] == ["T-1", "T-2"]
        assert data["tasks_blocked"] == ["T-3"]
        assert data["iterations"] == 14
        assert data["validation_runs"] == 3
        assert data["validation_failures"] == 1

    def test_counts_events_across_trace_rotation(self, tmp_session_root, tmp_path):
        import gzip

        path = rr_session.open_session()
        before = [
            {"ts": _ts(1), "event": "task_start", "task_id": "T-1"},
            {"ts": _ts(2), "event": "validate_end", "task_id": "T-1", "passed": True},
            {"ts": _ts(3), "event": "task_complete", "task_id": "T-1"},
        ]
        after = [
            {"ts": _ts(4), "event": "validate_end", "task_id": "T-2", "passed": False},
            {"ts": _ts(5), "event": "task_complete", "task_id": "T-2"},
            {"ts": _ts(6), "event": "task_block", "task_id": "T-3"},
        ]
        logs = tmp_path / "logs"
        # Rotation mid-session: archived events are gzipped, live file starts fresh.
        with gzip.open(logs / "trace.jsonl.20990101T000000_000000Z.gz", "wt") as f:
            for ev in before:
                f.write(json.dumps(ev) + "\n")
        _write_trace(tmp_path, after)
        rr_session.finalize_current()
        data = json.loads(path.read_text())

        assert data["tasks_completed"] == ["T-1", "T-2"]
        assert data["tasks_blocked"] == ["T-3"]
        assert data["validation_runs"] == 2
        assert data["validation_failures"] == 1

    def test_mid_rotation_twins_count_once(self, tmp_session_root, tmp_path):
        """Plain rotated file and a truncated .gz twin both on disk: count once."""
        import gzip

        path = rr_session.open_session()
        before = [
            {"ts": _ts(1), "event": "validate_end", "task_id": "T-1", "passed": True},
            {"ts": _ts(2), "event": "task_complete", "task_id": "T-1"},
        ]
        logs = tmp_path / "logs"
        plain = logs / "trace.jsonl.20990101T000000_000000Z"
        plain.write_text("".join(json.dumps(ev) + "\n" for ev in before))
        # Crash mid-gzip: the twin is a truncated stream holding the first event.
        gz = gzip.compress((json.dumps(before[0]) + "\n").encode())
        (logs / (plain.name + ".gz")).write_bytes(gz[: len(gz) - 4])
        _write_trace(tmp_path, [{"ts": _ts(3), "event": "task_complete", "task_id": "T-2"}])
        rr_session.finalize_current()
        data = json.loads(path.read_text())

        assert data["tasks_completed"] == ["T-1", "T-2"]
        assert data["validation_runs"] == 1

    def test_plain_twin_vanishing_mid_read_falls_back_to_gz(
        self, tmp_session_root, tmp_path, monkeypatch
    ):
        """Rotation unlinks the plain file after discovery: read the .gz instead."""
        import gzip

        path = rr_session.open_session()
        ev = {"ts": _ts(1), "event": "task_complete", "task_id": "T-1"}
        logs = tmp_path / "logs"
        plain = logs / "trace.jsonl.20990101T000000_000000Z"
        plain.write_text(json.dumps(ev) + "\n")
        with gzip.open(logs / (plain.name + ".gz"), "wt") as f:
            f.write(json.dumps(ev) + "\n")
        _write_trace(tmp_path, [])

        real = rr_session._trace_files
        calls = {"n": 0}

        def racing():
            files = real()
            calls["n"] += 1
            if calls["n"] == 1:
                plain.unlink()  # rotation finishes between listing and opening
            return files

        monkeypatch.setattr(rr_session, "_trace_files", racing)
        rr_session.finalize_current()
        data = json.loads(path.read_text())

        assert data["tasks_completed"] == ["T-1"]

    def test_live_rotated_and_recreated_mid_read_is_counted(
        self, tmp_session_root, tmp_path, monkeypatch
    ):
        """Live file renamed to an archive and recreated after discovery."""
        path = rr_session.open_session()
        logs = tmp_path / "logs"
        _write_trace(tmp_path, [{"ts": _ts(1), "event": "task_complete", "task_id": "T-1"}])
        live = logs / "trace.jsonl"

        real = rr_session._trace_files
        calls = {"n": 0}

        def racing():
            files = real()
            calls["n"] += 1
            if calls["n"] == 1:
                live.rename(logs / "trace.jsonl.20990101T000000_000000Z")
                live.write_text(
                    json.dumps({"ts": _ts(2), "event": "task_complete", "task_id": "T-2"})
                    + "\n"
                )
            return files

        monkeypatch.setattr(rr_session, "_trace_files", racing)
        rr_session.finalize_current()
        data = json.loads(path.read_text())

        assert data["tasks_completed"] == ["T-1", "T-2"]

    def test_retry_counter(self, tmp_session_root, tmp_path):
        rr_session.open_session()
        events = [
            {"ts": _ts(1), "event": "task_start", "task_id": "T-1"},
            {"ts": _ts(2), "event": "task_start", "task_id": "T-1"},  # retry 1
            {"ts": _ts(3), "event": "task_start", "task_id": "T-1"},  # retry 2
            {"ts": _ts(4), "event": "task_start", "task_id": "T-2"},  # not a retry
        ]
        _write_trace(tmp_path, events)
        path = rr_session.finalize_current()
        data = json.loads(path.read_text())
        assert data["retries"] == {"T-1": 2}

    def test_ignores_events_before_session_start(self, tmp_session_root, tmp_path):
        # Pre-existing trace events that predate this session must not be
        # rolled in.
        old = [
            {"ts": _ts(-3600), "event": "task_complete", "task_id": "OLD-1"},
            {"ts": _ts(-1800), "event": "task_block", "task_id": "OLD-2"},
        ]
        _write_trace(tmp_path, old)
        path = rr_session.open_session()
        # Now add an event in the session window.
        with open(tmp_path / "logs" / "trace.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _ts(1), "event": "task_complete", "task_id": "NEW-1"}) + "\n")
        rr_session.finalize_current()
        data = json.loads(path.read_text())
        assert data["tasks_completed"] == ["NEW-1"]
        assert "OLD-1" not in data["tasks_completed"]

    def test_clears_pointer(self, tmp_session_root):
        rr_session.open_session()
        assert rr_session.CURRENT_POINTER.exists()
        rr_session.finalize_current()
        assert not rr_session.CURRENT_POINTER.exists()


# ── list_sessions ────────────────────────────────────────────────────────────


class TestListSessions:
    def test_empty(self, tmp_session_root):
        assert rr_session.list_sessions() == []

    def test_excludes_currently_open(self, tmp_session_root):
        rr_session.open_session()  # leave it open
        assert rr_session.list_sessions() == []

    def test_newest_first(self, tmp_session_root):
        # Run three full sessions; finalize each before opening the next.
        rr_session.open_session()
        rr_session.finalize_current()
        rr_session.open_session()
        rr_session.finalize_current()
        rr_session.open_session()
        rr_session.finalize_current()
        results = rr_session.list_sessions()
        assert len(results) == 3
        # Newest first ⇒ started_at descending.
        starts = [r["started_at"] for r in results]
        assert starts == sorted(starts, reverse=True)

    def test_respects_limit(self, tmp_session_root):
        for _ in range(4):
            rr_session.open_session()
            rr_session.finalize_current()
        assert len(rr_session.list_sessions(limit=2)) == 2


# ── format_session ───────────────────────────────────────────────────────────


class TestFormatSession:
    def test_includes_key_fields(self):
        s = {
            "session_id": "abcd1234-rest",
            "started_at": "2026-04-25T10:00:00+00:00",
            "ended_at": "2026-04-25T10:11:30+00:00",
            "tasks_completed": ["X-1", "X-2"],
            "tasks_blocked": ["X-3"],
            "iterations": 17,
            "retries": {"X-1": 1},
            "validation_runs": 5,
            "validation_failures": 1,
            "commits": ["abc1234", "def5678"],
        }
        out = rr_session.format_session(s)
        assert "abcd1234" in out
        assert "X-1" in out and "X-2" in out
        assert "X-3" in out
        assert "11.5 min" in out
        assert "abc1234" in out


# ── health_line ──────────────────────────────────────────────────────────────


class TestHealthLine:
    def test_none_when_no_sessions(self, tmp_session_root):
        assert rr_session.health_line() is None

    def test_summarizes_last_finalized(self, tmp_session_root, tmp_path):
        rr_session.open_session()
        _write_trace(tmp_path, [
            {"ts": _ts(1), "event": "task_complete", "task_id": "T-1"},
            {"ts": _ts(2), "event": "task_complete", "task_id": "T-2"},
            {"ts": _ts(3), "event": "validate_end", "passed": False},
        ])
        rr_session.finalize_current()
        line = rr_session.health_line()
        assert line is not None
        assert "2 tasks" in line
        assert "1 validation failures" in line
