"""Acceptance for #45: invalid UTF-8 inside an oversized line still drops the archive.

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


@pytest.fixture
def logs(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    (d / "sessions").mkdir(parents=True)
    monkeypatch.setattr(rr_session, "TRACE_LOG", d / "trace.jsonl")
    monkeypatch.setattr(rr_session, "SESSIONS_DIR", d / "sessions")
    monkeypatch.setattr(rr_session, "CURRENT_POINTER", d / "sessions" / ".current")
    monkeypatch.setattr(rr_session, "_git_commits_in_window", lambda *a, **k: [])
    monkeypatch.setattr(rr_session, "MAX_TRACE_LINE_BYTES", 200)
    return d


def _run(logs, payload, gz):
    path = rr_session.open_session()
    name = logs / ("trace.jsonl.20990101T000000_000000Z" + (".gz" if gz else ""))
    name.write_bytes(gzip.compress(payload) if gz else payload)
    (logs / "trace.jsonl").write_bytes(_raw("LIVE"))
    rr_session.finalize_current()
    return json.loads(path.read_text())["tasks_completed"]


@pytest.mark.parametrize("gz", [False, True])
def test_invalid_utf8_in_oversized_line_drops_archive(logs, gz):
    # Invalid bytes sit past the first 201-byte read of a ~450-byte line.
    oversized = b"x" * 250 + b"\xff\xfe" + b"y" * 200 + b"\n"
    payload = _raw("BEFORE") + oversized + _raw("AFTER")
    assert _run(logs, payload, gz) == ["LIVE"]


@pytest.mark.parametrize("gz", [False, True])
def test_multibyte_char_split_across_chunks_is_valid(logs, gz):
    # A valid 4-byte character straddling a chunk boundary must not count as invalid.
    oversized = b"x" * 199 + "\U0001F600".encode() * 60 + b"\n"
    payload = _raw("BEFORE") + oversized + _raw("AFTER")
    assert _run(logs, payload, gz) == ["BEFORE", "AFTER", "LIVE"]
