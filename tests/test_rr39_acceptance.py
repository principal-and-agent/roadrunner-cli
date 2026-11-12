"""Acceptance for #39: replay limits are enforced on UTF-8 bytes, not characters.

Protected: the Roadrunner worker must not edit this file.
"""
import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from roadrunner import session as rr_session


@pytest.fixture
def root(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    (logs / "sessions").mkdir(parents=True)
    monkeypatch.setattr(rr_session, "TRACE_LOG", logs / "trace.jsonl")
    monkeypatch.setattr(rr_session, "SESSIONS_DIR", logs / "sessions")
    monkeypatch.setattr(rr_session, "CURRENT_POINTER", logs / "sessions" / ".current")
    monkeypatch.setattr(rr_session, "_git_commits_in_window", lambda *a, **k: [])
    return logs


def _ts(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _record(task_id):
    return json.dumps({"ts": _ts(5), "event": "task_complete", "task_id": task_id}, ensure_ascii=False)


def _finalize(root, lines):
    path = rr_session.open_session()
    with gzip.open(root / "trace.jsonl.20990101T000000_000000Z.gz", "wt", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    (root / "trace.jsonl").write_text("")
    rr_session.finalize_current()
    return json.loads(path.read_text())


def test_line_limit_counts_bytes(root, monkeypatch):
    # 4-byte emoji: ~70 chars but ~270 bytes. Under a 200-byte limit it must be dropped.
    wide = _record("W-" + "\U0001F600" * 50)
    assert len(wide) < 200 < len(wide.encode("utf-8"))
    monkeypatch.setattr(rr_session, "MAX_TRACE_LINE_BYTES", 200)
    data = _finalize(root, [wide, _record("OK-1")])
    assert data["tasks_completed"] == ["OK-1"]


def test_replay_budget_counts_bytes(root, monkeypatch):
    # Budget fits the first wide record by characters but not by bytes, so
    # nothing after it may be read.
    wide = _record("W-" + "\U0001F600" * 50)
    monkeypatch.setattr(rr_session, "MAX_REPLAY_BYTES", len(wide) + 5)
    data = _finalize(root, [wide, _record("LATE")])
    assert "LATE" not in data["tasks_completed"]


def test_ascii_unchanged(root):
    data = _finalize(root, [_record("A-1"), _record("A-2")])
    assert data["tasks_completed"] == ["A-1", "A-2"]
