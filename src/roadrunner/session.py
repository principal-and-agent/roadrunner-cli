"""
Session summary observability primitive.

A "session" is one `claude` invocation against the roadmap. Three things make
a session interesting after the fact:

  * what work landed (tasks completed, blocked, retried)
  * how much it took (iterations, validation cycles, wall time)
  * what shipped (commit SHAs)

trace.jsonl already records every event; this module rolls those events up
into one JSON file per session at ``logs/sessions/run_<iso8601>.json``, plus
exposes helpers to list and pretty-print recent summaries.

Design notes
------------
* The SessionStart hook drops a shell file with ``session_id`` + ``started_at``
  and points ``logs/sessions/.current`` at it. Subsequent events are not
  written incrementally — instead, ``finalize_current()`` runs once (on
  ROADMAP_COMPLETE, on iteration-cap fire, or when the next session opens)
  and reconstructs the summary by replaying trace events between the start
  marker and now plus reading ``git log`` for commit SHAs in that window.
  Reconstruction beats incremental writes for two reasons:
    1. The control loop never has to remember "did I bump validation_runs
       on that path?" — the trace is the source of truth and finalize is
       deterministic over it.
    2. A crash mid-session leaves the shell behind; the next SessionStart
       finalizes it from whatever trace exists, then opens a fresh one.

* Pure module: no import from ``cli`` to avoid cycles. Path
  constants are ``ROOT``-relative and rebindable in tests (same pattern as
  ``rr_state.STATE_FILE``).
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict, cast


# ── Paths ────────────────────────────────────────────────────────────────────

from .state import resolve_project_root

ROOT = resolve_project_root()
LOGS_DIR = ROOT / "logs"
TRACE_LOG = LOGS_DIR / "trace.jsonl"
SESSIONS_DIR = LOGS_DIR / "sessions"
CURRENT_POINTER = SESSIONS_DIR / ".current"


# ── Types ────────────────────────────────────────────────────────────────────


class SessionSummary(TypedDict, total=False):
    session_id: str
    started_at: str
    ended_at: str | None
    tasks_completed: list[str]
    tasks_blocked: list[str]
    iterations: int
    retries: dict[str, int]
    validation_runs: int
    validation_failures: int
    commits: list[str]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: tmp + fsync + os.replace.

    Same discipline as ``rr_state.write_state`` — never leave a half-written
    file behind if the process crashes mid-write.
    """
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _iso_for_filename(iso: str) -> str:
    """Make an ISO timestamp safe for a filename (strip ``:`` and ``+``)."""
    return iso.replace(":", "").replace("+", "").replace(".", "_")


def _trace_files() -> list[Path]:
    """Rotated trace archives (oldest first) followed by the live trace.

    Rotation renames ``trace.jsonl`` to ``trace.jsonl.<stamp>`` and gzips it, so
    a session that spans a rotation has events in several files. If both a rotated
    file and its ``.gz`` twin exist, only the plain file is read, so each event
    is counted once.
    """
    candidates = [
        p for p in TRACE_LOG.parent.glob(TRACE_LOG.name + ".*") if p.is_file()
    ]
    names = {p.name for p in candidates}
    # Rotation writes ``X.gz`` before unlinking ``X``. While both exist (mid-rotation,
    # or after a crash in that window) the plain file is the complete copy and the
    # .gz may be partial, so prefer the plain twin to avoid double counting.
    rotated = sorted(
        p for p in candidates
        if not (p.suffix == ".gz" and p.name[: -len(".gz")] in names)
    )
    live = [TRACE_LOG] if TRACE_LOG.exists() else []
    return rotated + live


def _read_file_events(path: Path) -> list[dict[str, Any]]:
    """Parse one trace file. Raises FileNotFoundError if it vanished."""
    opener = gzip.open if path.suffix == ".gz" else open
    events: list[dict[str, Any]] = []
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # A partially-written line at tail is the only realistic
                # cause; skip it rather than crash the summary.
                continue
    return events


def _read_trace_events(max_passes: int = 3) -> list[dict[str, Any]]:
    """Read the trace (rotated archives plus live file) into a list of dicts.

    If a file disappears between discovery and opening, a rotation moved it
    mid-read; rediscover and start over so its events are neither dropped nor
    double counted. A file that fails to parse (e.g. a truncated .gz with no
    plain twin) contributes nothing rather than a partial prefix.
    """
    out: list[dict[str, Any]] = []
    for _ in range(max_passes):
        out = []
        vanished = False
        for path in _trace_files():
            try:
                out.extend(_read_file_events(path))
            except FileNotFoundError:
                vanished = True
                break
            except (OSError, EOFError):
                continue
        if not vanished:
            return out
    return out


def _events_after(events: list[dict[str, Any]], cutoff_iso: str) -> list[dict[str, Any]]:
    """Filter events with ``ts`` > cutoff. Lexicographic ISO compare is correct
    when both strings are produced by ``datetime.isoformat()`` in UTC."""
    return [e for e in events if e.get("ts", "") > cutoff_iso]


def _git_commits_in_window(started_at: str, ended_at: str | None) -> list[str]:
    """Return short commit SHAs authored in [started_at, ended_at]. Falls back
    to empty list if git is unavailable or the cwd is not a repo."""
    cmd = ["git", "log", "--since", started_at, "--format=%h"]
    if ended_at:
        cmd.extend(["--until", ended_at])
    try:
        result = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ── Open / current / finalize ────────────────────────────────────────────────


def _shell_path_for(started_at: str, session_id: str) -> Path:
    """File name pattern: ``run_<iso>.json``. Session ID isn't in the name
    (kept inside the file) so the directory listing sorts cleanly by time."""
    return SESSIONS_DIR / f"run_{_iso_for_filename(started_at)}.json"


def open_session() -> Path:
    """Open a new session. If a prior session is still marked current, finalize
    it first (its ended_at will be "now"). Returns the new shell path.

    Idempotent enough: calling twice in quick succession will finalize the
    first, open a second; no orphaned pointers.
    """
    # Re-resolve module-level paths so test fixtures that monkeypatch them
    # are honored. We deliberately re-read from the module each call.
    _ensure_dir()
    finalize_current()  # closes prior if any
    started_at = _now()
    session_id = str(uuid.uuid4())
    shell: SessionSummary = {
        "session_id": session_id,
        "started_at": started_at,
        "ended_at": None,
        "tasks_completed": [],
        "tasks_blocked": [],
        "iterations": 0,
        "retries": {},
        "validation_runs": 0,
        "validation_failures": 0,
        "commits": [],
    }
    path = _shell_path_for(started_at, session_id)
    _atomic_write_json(path, dict(shell))
    CURRENT_POINTER.write_text(str(path))
    return path


def current_session_path() -> Path | None:
    """Path of the currently-open session, or None."""
    if not CURRENT_POINTER.exists():
        return None
    raw = CURRENT_POINTER.read_text().strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


def _load(path: Path) -> SessionSummary:
    with open(path, encoding="utf-8") as f:
        return cast(SessionSummary, json.load(f))


def finalize_current() -> Path | None:
    """Replay trace events since the open session's start, write the final
    summary, clear the pointer. No-op if no session is open. Returns the
    finalized path (or None)."""
    path = current_session_path()
    if path is None:
        return None
    summary = _load(path)
    started_at = summary.get("started_at", "")
    ended_at = _now()

    events = _events_after(_read_trace_events(), started_at)

    completed: list[str] = []
    blocked: list[str] = []
    retries: dict[str, int] = {}
    iterations = 0
    val_runs = 0
    val_failures = 0

    for ev in events:
        kind = ev.get("event")
        tid = ev.get("task_id")
        if kind == "task_complete" and tid:
            if tid not in completed:
                completed.append(tid)
        elif kind in ("task_block", "auto_block") and tid:
            if tid not in blocked:
                blocked.append(tid)
        elif kind == "validate_end":
            val_runs += 1
            if not ev.get("passed", True):
                val_failures += 1
        elif kind == "check_stop":
            it = ev.get("iteration")
            if isinstance(it, int) and it > iterations:
                iterations = it
        elif kind == "task_start" and tid:
            # A second task_start for the same id within one session = retry.
            retries[tid] = retries.get(tid, 0) + 1

    # task_start fires once per legitimate start; only count > 1 as a retry.
    retries = {k: v - 1 for k, v in retries.items() if v > 1}

    summary["ended_at"] = ended_at
    summary["tasks_completed"] = completed
    summary["tasks_blocked"] = blocked
    summary["iterations"] = iterations
    summary["retries"] = retries
    summary["validation_runs"] = val_runs
    summary["validation_failures"] = val_failures
    summary["commits"] = _git_commits_in_window(started_at, ended_at)

    _atomic_write_json(path, dict(summary))
    try:
        CURRENT_POINTER.unlink()
    except FileNotFoundError:
        pass
    return path


# ── List / format ────────────────────────────────────────────────────────────


def list_sessions(limit: int | None = None) -> list[SessionSummary]:
    """Return finalized session summaries newest-first. Excludes the
    currently-open session (it has no ended_at yet). ``limit=None`` returns
    all known sessions."""
    if not SESSIONS_DIR.exists():
        return []
    paths = sorted(SESSIONS_DIR.glob("run_*.json"), reverse=True)
    out: list[SessionSummary] = []
    for p in paths:
        try:
            s = _load(p)
        except (OSError, json.JSONDecodeError):
            continue
        if s.get("ended_at"):
            out.append(s)
            if limit is not None and len(out) >= limit:
                break
    return out


def _duration_minutes(s: SessionSummary) -> float | None:
    started = s.get("started_at")
    ended = s.get("ended_at")
    if not started or not ended:
        return None
    try:
        d = datetime.fromisoformat(ended) - datetime.fromisoformat(started)
    except ValueError:
        return None
    return round(d.total_seconds() / 60, 1)


def format_session(s: SessionSummary) -> str:
    """One-block pretty-print for a session. Used by the ``sessions`` command."""
    started = s.get("started_at", "?")
    ended = s.get("ended_at") or "(open)"
    mins = _duration_minutes(s)
    duration = f"{mins:.1f} min" if mins is not None else "?"
    completed = s.get("tasks_completed", [])
    blocked = s.get("tasks_blocked", [])
    retries = s.get("retries", {})
    commits = s.get("commits", [])
    val_runs = s.get("validation_runs", 0)
    val_failures = s.get("validation_failures", 0)
    iterations = s.get("iterations", 0)

    lines = [
        f"session {s.get('session_id', '?')[:8]}",
        f"  started: {started}",
        f"  ended:   {ended}  ({duration})",
        f"  tasks:   {len(completed)} completed, {len(blocked)} blocked",
    ]
    if completed:
        lines.append(f"    completed: {', '.join(completed)}")
    if blocked:
        lines.append(f"    blocked:   {', '.join(blocked)}")
    if retries:
        retry_str = ", ".join(f"{k}×{v + 1}" for k, v in retries.items())
        lines.append(f"  retries: {retry_str}")
    lines.append(
        f"  iterations: {iterations}  |  validations: {val_runs} runs, {val_failures} failures"
    )
    if commits:
        shown = ", ".join(commits[:8])
        more = f" (+{len(commits) - 8} more)" if len(commits) > 8 else ""
        lines.append(f"  commits: {shown}{more}")
    return "\n".join(lines)


def health_line() -> str | None:
    """One-line summary of the most recent finalized session, suitable for
    ``roadrunner health`` output. Returns None if no finalized sessions
    exist (don't pollute health output for fresh installs)."""
    last = list_sessions(limit=1)
    if not last:
        return None
    s = last[0]
    mins = _duration_minutes(s)
    duration = f"{mins:.1f} min" if mins is not None else "? min"
    return (
        f"last session: {len(s.get('tasks_completed', []))} tasks, "
        f"{duration}, {s.get('validation_failures', 0)} validation failures"
    )
