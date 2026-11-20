"""Acceptance for #43: strict shared replay budget; undecodable archives drop whole.

Protected: the Roadrunner worker must not edit this file.
"""
import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from roadrunner import session as rr_session


def _raw(task_id):
    ts = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    return (json.dumps({"ts": ts, "event": "task_complete", "task_id": task_id}) + "\n").encode()


def _ids(events):
    return [e["task_id"] for e in events]


def test_budget_one_byte_short_rejects_second_record_across_files(tmp_path):
    first, second = _raw("FIRST"), _raw("SECOND")
    a, b = tmp_path / "trace.jsonl.1", tmp_path / "trace.jsonl"
    a.write_bytes(first)
    b.write_bytes(second)
    budget = rr_session._ReplayBudget(len(first) + len(second) - 1)
    got = _ids(rr_session._read_file_events(a, budget)) + _ids(rr_session._read_file_events(b, budget))
    assert got == ["FIRST"]
    assert budget.remaining >= 0


def test_budget_exact_fit_accepts_both(tmp_path):
    first, second = _raw("FIRST"), _raw("SECOND")
    a = tmp_path / "trace.jsonl"
    a.write_bytes(first + second)
    budget = rr_session._ReplayBudget(len(first) + len(second))
    assert _ids(rr_session._read_file_events(a, budget)) == ["FIRST", "SECOND"]
    assert budget.remaining == 0


def test_single_file_budget_never_negative(tmp_path):
    first, second = _raw("FIRST"), _raw("SECOND")
    a = tmp_path / "trace.jsonl"
    a.write_bytes(first + second)
    budget = rr_session._ReplayBudget(len(first) + 3)
    assert _ids(rr_session._read_file_events(a, budget)) == ["FIRST"]
    assert budget.remaining >= 0


def test_line_limit_boundary(tmp_path, monkeypatch):
    rec = _raw("EXACT")
    monkeypatch.setattr(rr_session, "MAX_TRACE_LINE_BYTES", len(rec))
    a = tmp_path / "trace.jsonl"
    a.write_bytes(rec)
    assert _ids(rr_session._read_file_events(a, rr_session._ReplayBudget())) == ["EXACT"]
    monkeypatch.setattr(rr_session, "MAX_TRACE_LINE_BYTES", len(rec) - 1)
    assert _ids(rr_session._read_file_events(a, rr_session._ReplayBudget())) == []


@pytest.mark.parametrize("gz", [False, True])
def test_undecodable_archive_dropped_whole(tmp_path, monkeypatch, gz):
    logs = tmp_path / "logs"
    (logs / "sessions").mkdir(parents=True)
    monkeypatch.setattr(rr_session, "TRACE_LOG", logs / "trace.jsonl")
    monkeypatch.setattr(rr_session, "SESSIONS_DIR", logs / "sessions")
    monkeypatch.setattr(rr_session, "CURRENT_POINTER", logs / "sessions" / ".current")
    monkeypatch.setattr(rr_session, "_git_commits_in_window", lambda *a, **k: [])
    path = rr_session.open_session()
    payload = _raw("BEFORE") + b"\xff\xfe not utf-8\n" + _raw("AFTER")
    name = logs / ("trace.jsonl.20990101T000000_000000Z" + (".gz" if gz else ""))
    name.write_bytes(gzip.compress(payload) if gz else payload)
    (logs / "trace.jsonl").write_bytes(_raw("LIVE"))
    rr_session.finalize_current()
    assert json.loads(path.read_text())["tasks_completed"] == ["LIVE"]
