#!/usr/bin/env python3
"""
Roadmap Loop Controller
-----------------------
Python owns: task selection, dependency resolution, validation, logging, state.
Claude Code owns: implementation inside one task boundary.
Hooks own: stop enforcement, completion gating, context snapshots.
"""

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict, cast

import yaml

from . import session as rr_session  # session summary observability (Issue 6)

# State persistence lives in rr_state.py — extracted as a bounded module so
# the highest-risk section of the loop (atomic writes, advisory locking,
# schema versioning) has its own blast radius. See Issue 5 of the 2026-04-24
# resolution plan. The names below are re-exported so the public
# ``roadrunner`` import surface is unchanged.
from .state import resolve_project_root
from .state import (  # noqa: F401 — re-exports
    STATE_FILE,
    STATE_LOCK,
    STATE_SCHEMA_VERSION,
    RoadmapState,
    _exclusive_state_lock,
    increment_attempts,
    read_state,
    write_state,
)

try:
    import fcntl  # POSIX advisory locking for the state file; Windows is not a target platform.
except ImportError:  # pragma: no cover - Windows fallback keeps the module importable
    fcntl = None  # type: ignore[assignment]


# ── Type aliases ─────────────────────────────────────────────────────────────
# These are structural hints only — every function still accepts plain dicts
# so callers (including tests) need no changes. They exist so editors flag
# key typos and make the implicit schema explicit.


class Task(TypedDict, total=False):
    id: str
    title: str
    status: str  # "todo" | "in_progress" | "done" | "blocked"
    depends_on: list[str]
    goal: str
    acceptance_criteria: list[str]
    validation_commands: list[str]
    validation_timeout: int
    files_expected: list[str]
    documentation_targets: list[str]
    notes: str
    model: str  # ROAD-012: optional per-task model hint (e.g. "claude-haiku-4-5")


# RoadmapState is defined in rr_state.py and re-exported at the top of this
# module. Kept the class name in the public namespace for tests and any
# downstream consumers that did `from roadrunner import RoadmapState`. The
# `session_iteration` field (ROAD-010, schema v2) lives there too.


class ValidationResult(TypedDict, total=False):
    command: str
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    kind: str  # failures only: "fail" | "timeout" | "env" | "vacuous" (see classify_validation)
    vacuous: bool  # passed, but ran nothing that asserts anything (issue #18)
    phase: str  # ROAD-015: "baseline" or "task" — which suite the command ran in


# ── Paths ────────────────────────────────────────────────────────────────────

ROOT = resolve_project_root()
TASKS_FILE = ROOT / "tasks" / "tasks.yaml"
LOGS_DIR = ROOT / "logs"
CHANGELOG = LOGS_DIR / "CHANGELOG.md"
TRACE_LOG = LOGS_DIR / "trace.jsonl"
LEARNINGS_FILE = LOGS_DIR / "learnings.md"  # ROAD-013: operator learnings log
TASKS_BACKUP = TASKS_FILE.with_suffix(".yaml.bak")
# STATE_FILE and STATE_LOCK are owned by state.py and re-exported above.
try:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # ROOT may be a non-existent path at import time (CLAUDE_PROJECT_DIR
    # pointing somewhere stale, or cwd-fallback in an unwritable dir).
    # Commands that actually write logs will surface a clearer error at
    # call time instead of taking down ``import roadrunner`` for callers
    # that just want to inspect the package.
    pass


# ── Tunables ─────────────────────────────────────────────────────────────────
# Collected here so operators have one place to adjust retention and safety knobs.

DEFAULT_VALIDATION_TIMEOUT = 300  # seconds; per-task override via validation_timeout in tasks.yaml
MAX_TASK_ATTEMPTS = 5  # auto-block a task after this many resume cycles without completion
TASKS_BACKUP_KEEP = 5  # number of rolling tasks.yaml.bak.N copies to retain
LOG_ROTATE_BYTES = 10 * 1024 * 1024  # rotate when a log file exceeds 10 MB
LOG_RETAIN_DAYS = 7  # delete rotated/compressed logs older than this
# STATE_SCHEMA_VERSION is owned by rr_state.py and re-exported above.
# v2 (ROAD-010) adds the session_iteration field; older state files migrate
# transparently via setdefault in rr_state.read_state.
# v3 (ROAD-011) adds the session_cost_usd field; same migration path.
SNAPSHOT_SCHEMA_VERSION = 1  # bump when .context_snapshot.json format changes incompatibly

# ROAD-013: cap on how many learnings lines get folded into the SessionStart
# additionalContext. Twenty is enough to preserve recent operational facts
# without bloating the agent's bootstrap context — older entries stay in the
# file for the operator to read manually.
LEARNINGS_TAIL_LINES = 20


def _learnings_entries() -> list[str]:
    """Return the non-blank, non-comment learnings entries in append order.

    Lines starting with ``#`` are header markdown; lines starting with ``<!--``
    are template placeholders. Skipping both keeps the count and the tail
    aligned with what the operator considers an actual entry.
    """
    if not LEARNINGS_FILE.exists():
        return []
    try:
        raw = LEARNINGS_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
            continue
        out.append(stripped)
    return out


def _learnings_tail(limit: int = LEARNINGS_TAIL_LINES) -> list[str]:
    """Last ``limit`` learnings entries, oldest-first within the tail."""
    entries = _learnings_entries()
    if not entries:
        return []
    return entries[-limit:]


# ROAD-012: known model prefixes / aliases for the optional per-task `model:`
# field. The validator is intentionally permissive — it warns (not errors) on
# unknown values so a newly-released model ID never breaks tasks.yaml loading.
# Add new families here; specific point releases are matched by prefix.
_KNOWN_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-",  # Anthropic Claude family (opus / sonnet / haiku, all generations)
    "gpt-",  # OpenAI GPT family
    "o1-",
    "o3-",
    "o4-",  # OpenAI reasoning models
    "gemini-",  # Google Gemini
    "llama-",  # Meta Llama
    "mistral-",
    "mixtral-",  # Mistral
    "deepseek-",
    "qwen-",
)
_KNOWN_MODEL_ALIASES: frozenset[str] = frozenset(
    {
        "haiku",
        "sonnet",
        "opus",  # Anthropic short names commonly used in operator scripts
    }
)

# Module-level dedupe for the unknown-model warning so each unrecognized ID is
# reported once per process rather than spamming on every load_tasks() call.
_unknown_model_warnings_seen: set[str] = set()


# ROAD-011: one-time stderr warning when a budget is configured but no per-turn
# cost data is observed in the Stop-hook payload. Module-level so it deduplicates
# across check-stop fires within a single process; the typical loop is one
# process per Stop fire so this trips on the first fire and stays quiet for the
# remainder of the session by virtue of the loop's natural cadence.
_cost_data_warning_emitted = False

# ROAD-011: candidate keys in the Stop-hook stdin payload for per-session cost
# data. Claude Code's hook schema does not yet stabilize on a single name, so
# we look at several plausible shapes and take the first one that parses as a
# float. The shapes are nested-tolerant: ``usage.total_cost_usd`` is treated
# the same as a top-level ``total_cost_usd``.
_COST_KEY_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("total_cost_usd",),
    ("cost_usd",),
    ("session_cost_usd",),
    ("usage", "total_cost_usd"),
    ("usage", "cost_usd"),
    ("cost", "total_usd"),
    ("cost", "usd"),
)


def _extract_session_cost_usd(payload: dict) -> float | None:
    """Pull a session-cumulative USD cost out of the Stop-hook stdin payload.

    Returns ``None`` if no recognised shape is present so the caller can keep
    the previously-persisted value rather than zeroing it on every fire. Any
    non-numeric value is treated as missing rather than raising — the budget
    feature must never break the control loop on a malformed payload.
    """
    for path in _COST_KEY_CANDIDATES:
        node: Any = payload
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            return float(node)
    return None


def _resolve_max_budget_usd(arg_value: Any) -> float | None:
    """Resolve --max-budget-usd from the CLI flag, falling back to the
    ``ROADMAP_MAX_BUDGET_USD`` environment variable. Returns ``None`` when
    the feature is disabled (neither source set). Non-numeric values are
    treated as disabled so a typo doesn't silently halt the loop.
    """
    candidates: tuple[Any, ...] = (arg_value, os.environ.get("ROADMAP_MAX_BUDGET_USD"))
    for raw in candidates:
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


# Built from two fragments so the sentinel string never appears literally in the source
# (protects against `is_completion_signal` misfiring on the file that defines it).
_COMPLETION_SIGNAL = "ROADMAP" + "_COMPLETE"

# ── YAML helpers ─────────────────────────────────────────────────────────────


REQUIRED_TASK_FIELDS = {"id", "status", "title"}
VALID_TASK_STATUSES = {"todo", "in_progress", "done", "blocked"}
TASK_ID_RE = re.compile(r"^[A-Z]+-\d+$")


def validate_task_schema(task: dict, index: int) -> None:
    missing = REQUIRED_TASK_FIELDS - set(task.keys())
    if missing:
        task_id = task.get("id", f"<index {index}>")
        raise ValueError(f"Task {task_id} is missing required fields: {', '.join(sorted(missing))}")
    task_id = task.get("id", "")
    if not TASK_ID_RE.match(str(task_id)):
        raise ValueError(f"Task {task_id!r} has invalid ID format. Must match [A-Z]+-\\d+ (e.g., TASK-001)")
    status = task.get("status")
    if status not in VALID_TASK_STATUSES:
        raise ValueError(f"Task {task['id']}: invalid status {status!r}. Must be one of: {', '.join(sorted(VALID_TASK_STATUSES))}")
    if not isinstance(task.get("validation_commands", []), list):
        raise ValueError(f"Task {task['id']}: validation_commands must be a list")
    if not isinstance(task.get("depends_on", []), list):
        raise ValueError(f"Task {task['id']}: depends_on must be a list")
    timeout = task.get("validation_timeout")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise ValueError(f"Task {task['id']}: validation_timeout must be a positive number")
    # ROAD-012: per-task model hint. Type-strict (must be a string if present)
    # but value-permissive (unknown IDs warn, never error) so a newly-released
    # model ID never breaks tasks.yaml loading.
    model = task.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"Task {task['id']}: model must be a non-empty string")
        if not _is_known_model(model) and model not in _unknown_model_warnings_seen:
            _unknown_model_warnings_seen.add(model)
            print(
                f"[roadrunner] task {task['id']}: model {model!r} is not in the "
                f"known-model list; treating as advisory. Add to "
                f"_KNOWN_MODEL_PREFIXES in src/roadrunner/cli.py if this is a "
                f"valid new release.",
                file=sys.stderr,
            )


def _is_known_model(model_id: str) -> bool:
    """ROAD-012: return True for known model IDs (prefix or alias match).

    Intentionally permissive — used only to suppress the unknown-model warning,
    not to gate loading. A False return is informational, never fatal.
    """
    normalized = model_id.strip().lower()
    if normalized in _KNOWN_MODEL_ALIASES:
        return True
    return any(normalized.startswith(prefix) for prefix in _KNOWN_MODEL_PREFIXES)


def load_tasks() -> list[Task]:
    try:
        with open(TASKS_FILE) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"tasks file not found at {TASKS_FILE}. Create it before running any command.") from exc
    except yaml.YAMLError as exc:
        # Hard stop: without a valid queue the loop cannot make decisions.
        raise ValueError(
            f"tasks file at {TASKS_FILE} is not valid YAML: {exc}. Check tasks/tasks.yaml.bak for the last known-good version."
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("tasks", []), list):
        raise ValueError("Plan must contain a tasks list")
    tasks = data.get("tasks", [])
    for i, task in enumerate(tasks):
        validate_task_schema(task, i)
    validate_plan(tasks)
    return cast(list[Task], tasks)


def validate_plan(tasks: list) -> None:
    ids = [task["id"] for task in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate task IDs")
    deps = {task["id"]: task.get("depends_on", []) for task in tasks}
    for task_id, prerequisites in deps.items():
        if any(dep not in deps for dep in prerequisites):
            raise ValueError(f"Task {task_id} has unknown dependencies")
    if _find_cycles(ids, deps):
        raise ValueError("Circular task dependencies")
    if sum(task.get("status") == "in_progress" for task in tasks) > 1:
        raise ValueError("Only one active task is permitted")


def load_project_config() -> dict:
    """Read top-level config keys from tasks.yaml (everything except `tasks`).

    Returns {} if the file is missing or unparseable — this helper is best-effort
    and never raises, so reads can be done defensively from any code path.
    """
    try:
        with open(TASKS_FILE) as f:
            data = yaml.safe_load(f) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k != "tasks"}


def get_baseline_validation() -> list[str]:
    """Read the optional top-level ``baseline_validation`` list from tasks.yaml.

    Returns the list of shell commands that run before every task's own
    ``validation_commands``. If the field is absent, empty, or malformed, returns
    an empty list — backward compat for projects without a baseline.

    The baseline is the project-wide CI-equivalent gate (ROAD-015): ``run_validation``
    runs it first, short-circuits on first failure, and only then runs the task's
    own commands. Set this to the exact invocations your CI workflow uses to make
    the loop's validate gate structurally equal to CI. Intentionally language-
    agnostic — Python projects set pytest/ruff/mypy; TypeScript projects set
    npm test, eslint, npx tsc --noEmit; the runtime treats them as opaque shell
    commands.
    """
    raw = load_project_config().get("baseline_validation")
    if not isinstance(raw, list):
        return []
    return [str(c).strip() for c in raw if isinstance(c, str) and c.strip()]


def get_project_base() -> str:
    """Resolve the base branch task branches should fork from.

    Priority:
      1. `project_base` key in tasks.yaml (top-level) — explicit config, preferred
      2. Current git branch at the moment of call — legacy behavior pre-ROAD-025
      3. "main" — final fallback

    Setting `project_base` in tasks.yaml is how users opt out of the stacking
    pattern where task branches fork from whatever previous task branch was HEAD.
    """
    configured = load_project_config().get("project_base")
    if isinstance(configured, str) and configured:
        return configured
    return _current_branch() or "main"


def _rotate_task_backups() -> None:
    """Shift rolling backups: .bak.N-1 → .bak.N, overwriting the oldest atomically.

    Order (newest → oldest): tasks.yaml.bak, tasks.yaml.bak.1, ..., tasks.yaml.bak.N.
    Uses ``Path.replace`` so each rename atomically evicts the slot it lands in — no
    unconditional ``unlink`` of the oldest, which would create a window where the
    oldest snapshot is gone with nothing to replace it.
    """
    for i in range(TASKS_BACKUP_KEEP - 1, 0, -1):
        src = TASKS_FILE.with_suffix(f".yaml.bak.{i}")
        dst = TASKS_FILE.with_suffix(f".yaml.bak.{i + 1}")
        if src.exists():
            src.replace(dst)
    if TASKS_BACKUP.exists():
        TASKS_BACKUP.replace(TASKS_FILE.with_suffix(".yaml.bak.1"))


def save_tasks(tasks: list[Task]) -> None:
    # Tolerate an empty or transiently-missing 'tasks:' section by defaulting
    # to a fresh wrapper dict; re-loading existing top-level keys preserves
    # any other fields in tasks.yaml the operator may have added.
    with open(TASKS_FILE) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        data = {}
    data["tasks"] = tasks

    # Stage the new content first. If serialization fails, nothing above the
    # temp file is disturbed — backups retain whatever they had.
    tmp_path = TASKS_FILE.with_suffix(TASKS_FILE.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())

    # Only rotate the backup chain once we know the new content is on disk
    # and ready to replace. This keeps the rolling chain crash-safe against
    # any failure in the dump/fsync step above.
    if TASKS_FILE.exists():
        _rotate_task_backups()
        shutil.copy2(TASKS_FILE, TASKS_BACKUP)
    os.replace(tmp_path, TASKS_FILE)


def get_task(tasks: list[Task], task_id: str) -> Task | None:
    return next((t for t in tasks if t["id"] == task_id), None)


# ── Eligibility ───────────────────────────────────────────────────────────────


def is_eligible(task: Task, tasks: list[Task]) -> bool:
    if task.get("status") != "todo":
        return False
    for dep_id in task.get("depends_on", []):
        dep = get_task(tasks, dep_id)
        if not dep or dep.get("status") != "done":
            return False
    return True


def next_eligible_task(tasks: list[Task]) -> Task | None:
    return next((t for t in tasks if is_eligible(t, tasks)), None)


def active_task(tasks: list[Task]) -> Task | None:
    return next((t for t in tasks if t.get("status") == "in_progress"), None)


ROADMAP_COMPLETE_RE = re.compile(r"^\s*ROADMAP_COMPLETE\s*$")


def is_completion_signal(last_msg: str) -> bool:
    """True only if the last non-empty line of last_msg is ROADMAP_COMPLETE."""
    if not last_msg:
        return False
    for line in reversed(last_msg.splitlines()):
        if line.strip():
            return bool(ROADMAP_COMPLETE_RE.match(line))
    return False


# ── Git helpers (partial-work recovery) ──────────────────────────────────────


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=check,
    )


def _is_git_repo() -> bool:
    return _git("rev-parse", "--is-inside-work-tree", check=False).returncode == 0


def _current_branch() -> str | None:
    result = _git("rev-parse", "--abbrev-ref", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _branch_exists(branch: str) -> bool:
    return _git("rev-parse", "--verify", branch, check=False).returncode == 0


def task_branch_name(task_id: str) -> str:
    return f"roadrunner/{task_id}"


def create_task_branch(task_id: str, base_branch: str | None = None) -> bool:
    """Create a task branch. Returns True if created (or already exists), False on git errors.

    If base_branch is provided and exists, checks it out BEFORE creating the task
    branch — this prevents stacking. Without this, calling start on a new task while
    HEAD is still on a previous task branch would create the new branch atop the old,
    producing a stack instead of a fan-out from the project base.
    """
    if not _is_git_repo():
        return False
    branch = task_branch_name(task_id)
    if _branch_exists(branch):
        return _git("checkout", branch, check=False).returncode == 0
    if base_branch and not _branch_exists(base_branch):
        return False
    if base_branch and _branch_exists(base_branch):
        checkout = _git("checkout", base_branch, check=False)
        if checkout.returncode != 0:
            trace_event(
                "git_base_checkout_error",
                task_id=task_id,
                extra={"base": base_branch, "stderr": checkout.stderr.strip()[:200]},
            )
            return False
    result = _git("checkout", "-b", branch, check=False)
    if result.returncode != 0:
        trace_event("git_branch_error", task_id=task_id, extra={"stderr": result.stderr.strip()[:200]})
        return False
    trace_event("git_branch_create", task_id=task_id, extra={"branch": branch, "base": base_branch})
    return True


def _push_branch(branch: str, task_id: str | None = None) -> bool:
    """Push a branch to origin. Non-fatal: logs to trace and prints a warning
    on failure (no authentication, protected branch, conflict, etc.) and
    returns False. Never raises, never exits — push failures must not fail
    a task that's otherwise valid."""
    result = _git("push", "origin", branch, check=False)
    passed = result.returncode == 0
    trace_event(
        "git_push",
        task_id=task_id,
        extra={
            "branch": branch,
            "returncode": result.returncode,
            "stderr": "" if passed else result.stderr.strip()[:200],
        },
    )
    if not passed:
        print(
            f"⚠️  Push to origin/{branch} failed: {result.stderr.strip()[:200]}",
            file=sys.stderr,
        )
    return passed


def _configured_push_mode() -> str:
    """Return push_on_complete from tasks.yaml config. Defaults to 'none'
    for back-compat with existing projects that haven't opted in."""
    mode = load_project_config().get("push_on_complete", "none")
    if not isinstance(mode, str):
        return "none"
    mode = mode.lower().strip()
    if mode not in ("none", "base", "task", "both"):
        return "none"
    return mode


def merge_task_branch(task_id: str, base_branch: str) -> bool:
    """Merge task branch back to base and delete it. Returns True on success.

    On merge failure (e.g. conflict), runs `git merge --abort` so the repo is
    returned to a clean state. The task branch is left intact for manual
    resolution.

    After a successful merge, honors `push_on_complete` from tasks.yaml:
      - `base` — push origin/<base_branch>
      - `task` — push origin/<task_branch> before deleting it
      - `both` — push both
      - `none` (default) — no push, local-only
    Push failures are non-fatal; see _push_branch.
    """
    if not _is_git_repo():
        return False
    branch = task_branch_name(task_id)
    if not _branch_exists(branch):
        return False
    if _git("checkout", base_branch, check=False).returncode != 0:
        return False
    result = _git("merge", branch, "--no-edit", check=False)
    if result.returncode != 0:
        # Abort the in-flight merge so callers can inspect/retry cleanly.
        abort = _git("merge", "--abort", check=False)
        trace_event(
            "git_merge_error",
            task_id=task_id,
            extra={
                "stderr": result.stderr.strip()[:200],
                "branch": branch,
                "base": base_branch,
            },
        )
        # Record the abort independently so a double-failure (abort itself
        # errored, e.g. no merge was actually in progress) is visible in the trace.
        trace_event(
            "git_merge_abort",
            task_id=task_id,
            extra={
                "returncode": abort.returncode,
                "stderr": abort.stderr.strip()[:200],
                "branch": branch,
            },
        )
        return False
    push_mode = _configured_push_mode()
    # Push task branch BEFORE deleting it locally.
    if push_mode in ("task", "both"):
        _push_branch(branch, task_id=task_id)
    _git("branch", "-d", branch, check=False)
    trace_event("git_branch_merge", task_id=task_id, extra={"branch": branch, "into": base_branch})
    if push_mode in ("base", "both"):
        _push_branch(base_branch, task_id=task_id)
    return True


# ── Validation ────────────────────────────────────────────────────────────────


# Shell exit codes meaning the command never ran: 127 not found, 126 not executable.
_ENV_RETURNCODES = frozenset({126, 127})


def classify_validation(returncode: int, timed_out: bool) -> str:
    """Classify a failed validation command.

    ``timeout``: killed by the validation timeout. ``env``: the shell could
    not run it (missing or non-executable binary), a setup problem and not a
    code problem. ``fail``: it ran and exited nonzero. Heuristic: a script
    that itself exits 126/127 is reported as ``env``.
    """
    if timed_out:
        return "timeout"
    if returncode in _ENV_RETURNCODES:
        return "env"
    return "fail"


def _kind_note(r: ValidationResult) -> str:
    kind = r.get("kind")
    if kind == "timeout":
        return "timed out (raise validation_timeout or check for a hang), not a test failure"
    if kind == "env":
        return "command could not run (missing or non-executable), environment problem not a code failure"
    if kind == "vacuous":
        return "coverage floor not met: the checks that passed verified nothing, so this is not a pass"
    return ""


DEFAULT_COVERAGE_FLOOR = 1

# Commands that exit 0 without checking anything.
_NOOP_COMMANDS = frozenset({"true", ":", "echo", "printf", "exit"})
# Test runners that report an empty run on stdout and may still exit 0
# (wrapper scripts, ``|| true``, runners configured not to fail on empty).
_EMPTY_RUN_RE = re.compile(r"\bno tests ran\b|collected 0 items|\bRan 0 tests\b|\bNo tests found\b", re.IGNORECASE)


def is_vacuous(cmd: str, stdout: str) -> bool:
    """True when a command that exited 0 verified nothing (issue #18).

    Deterministic and conservative: a bare no-op command (``true``, ``echo``),
    or output where a test runner says it ran no tests. Anything else counts
    as substantive.
    """
    parts = cmd.strip().split(None, 1)
    if parts and parts[0] in _NOOP_COMMANDS:
        return True
    return bool(_EMPTY_RUN_RE.search(stdout))


def get_coverage_floor(task: Task) -> int:
    """Minimum substantive passing checks required. Task key overrides the
    top-level ``coverage_floor`` in tasks.yaml; default 1; 0 disables."""
    for raw in (task.get("coverage_floor"), load_project_config().get("coverage_floor")):
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            return raw
    return DEFAULT_COVERAGE_FLOOR

VALIDATION_OUTPUT_KEEP = 1500  # chars kept from each end of captured validation output


def _head_tail(text: str, keep: int = VALIDATION_OUTPUT_KEEP) -> str:
    """Strip ``text``; if longer than 2*keep keep head and tail with a marker.

    The tail matters most: pytest's summary line and the last traceback
    frames are at the end, which a head-only cut discards.
    """
    text = text.strip()
    if len(text) <= 2 * keep:
        return text
    omitted = len(text) - 2 * keep
    return f"{text[:keep]}\n... [{omitted} chars omitted] ...\n{text[-keep:]}"


def run_validation(task: Task) -> tuple[bool, list[ValidationResult]]:
    """Run project baseline (if any), then the task's validation_commands.

    ROAD-015: a project-wide ``baseline_validation`` list at the top of
    tasks.yaml runs as a gate before each task's per-task commands. The
    baseline short-circuits on first failure — if pytest in the baseline
    fails, ruff/mypy/task-specific don't run. This is the structural
    "local equals CI" guarantee: no task can reach ``complete`` if any
    baseline command fails. Within task-specific commands the prior
    continue-on-failure behavior is preserved so the operator still sees
    every task-level failure at once.

    Each ValidationResult carries a ``phase`` field (``baseline`` |
    ``task``) so callers can render the two suites distinctly.
    """
    import time

    baseline = get_baseline_validation()
    task_commands = task.get("validation_commands", [])
    if not baseline and not task_commands:
        return True, []

    results: list[ValidationResult] = []
    state = read_state()
    timeout = task.get("validation_timeout", DEFAULT_VALIDATION_TIMEOUT)

    def _run_one(cmd: str, phase: str) -> bool:
        t0 = time.monotonic()
        timed_out = False
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=timeout,
            )
            passed = result.returncode == 0
            stdout = _head_tail(result.stdout)
            stderr = _head_tail(result.stderr)
            returncode = result.returncode
        except subprocess.TimeoutExpired as exc:
            passed = False
            timed_out = True
            stdout = _head_tail((exc.stdout or b"").decode(errors="replace"))
            stderr = _head_tail((exc.stderr or b"").decode(errors="replace"))
            returncode = -1
        elapsed = (time.monotonic() - t0) * 1000
        entry: ValidationResult = {
            "command": cmd,
            "passed": passed,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "phase": phase,
        }
        if timed_out:
            entry["timed_out"] = True
        if passed and is_vacuous(cmd, stdout):
            entry["vacuous"] = True
        kind = None if passed else classify_validation(returncode, timed_out)
        if kind:
            entry["kind"] = kind
        results.append(entry)
        trace_event(
            "validation_command",
            task_id=task["id"],
            iteration=state.get("iteration"),
            command=cmd,
            exit_code=returncode,
            duration_ms=elapsed,
            extra={"phase": phase, "kind": kind},
        )
        return passed

    baseline_passed = True
    # Phase 1: baseline — gate. First failure stops the rest of validation.
    for cmd in baseline:
        if not _run_one(cmd, "baseline"):
            baseline_passed = False
            break

    # Phase 2: task-specific. Only runs if baseline passed (short-circuit
    # semantics). Within this phase we continue on failure so the operator
    # sees every task-level issue at once, matching the pre-ROAD-015 UX.
    task_passed = True
    if baseline_passed:
        for cmd in task_commands:
            if not _run_one(cmd, "task"):
                task_passed = False

    all_passed = baseline_passed and task_passed
    # Coverage floor (issue #18): "ok" (nothing that ran failed) is not
    # "verified" (enough checks that assert something actually passed).
    floor = get_coverage_floor(task)
    substantive = sum(1 for r in results if r["passed"] and not r.get("vacuous"))
    if all_passed and substantive < floor:
        all_passed = False
        task_passed = False
        results.append(
            {
                "command": "<coverage floor>",
                "passed": False,
                "returncode": 0,
                "stdout": "",
                "stderr": (
                    f"only {substantive} substantive passing check(s), floor is {floor}. "
                    "Checks like true/echo or an empty test run do not count; add a real "
                    "validation command or lower coverage_floor."
                ),
                "phase": "task",
                "kind": "vacuous",
            }
        )
    trace_event(
        "validation_complete",
        task_id=task["id"],
        iteration=state.get("iteration"),
        extra={
            "passed": all_passed,
            "total": len(results),
            "baseline_passed": baseline_passed,
            "task_passed": task_passed,
            "substantive": substantive,
            "coverage_floor": floor,
        },
    )
    return all_passed, results


# ── State management ──────────────────────────────────────────────────────────
# write_state, read_state, increment_attempts, and _exclusive_state_lock are
# owned by rr_state.py (Issue 5 extraction) and re-exported at the top of this
# module. RoadmapState, STATE_FILE, STATE_LOCK, and STATE_SCHEMA_VERSION are
# also re-exported. Public API is unchanged.


# ── Logging ───────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_changelog(task_id: str, status: str, notes: str = "") -> None:
    entry = f"## {_now()} | {task_id} → {status}\n{notes}\n\n"
    try:
        with open(CHANGELOG, "a") as f:
            f.write(entry)
    except OSError as exc:
        # Audit trail must never break the control loop.
        print(f"[roadrunner] changelog append failed: {exc}", file=sys.stderr)


def trace_event(
    event_type: str,
    task_id: str | None = None,
    iteration: int | None = None,
    command: str | None = None,
    exit_code: int | None = None,
    duration_ms: float | None = None,
    extra: dict | None = None,
) -> None:
    record: dict[str, Any] = {
        "ts": _now(),
        "event": event_type,
        "task_id": task_id,
        "iteration": iteration,
    }
    if command is not None:
        record["command"] = command
    if exit_code is not None:
        record["exit_code"] = exit_code
    if duration_ms is not None:
        record["duration_ms"] = round(duration_ms, 1)
    if extra:
        record.update(extra)
    try:
        with open(TRACE_LOG, "a") as f:
            f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
    except OSError as exc:
        # Observability must never break the control loop.
        print(f"[roadrunner] trace_event failed: {exc}", file=sys.stderr)


# ── Log rotation ──────────────────────────────────────────────────────────────


def _rotate_one(path: Path) -> None:
    """Rotate a single log file by renaming with a UTC timestamp suffix and gzipping.

    The suffix includes microseconds and falls back to a counter on collision so two
    rotations within the same microsecond never clobber each other's archives.
    """
    if not path.exists() or path.stat().st_size < LOG_ROTATE_BYTES:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    rotated = path.with_name(f"{path.name}.{stamp}")
    # Collision safety: if the rotated name or its .gz twin already exists
    # (same-microsecond rotations on a clock with coarser-than-μs resolution),
    # append a monotonic counter.
    counter = 1
    while rotated.exists() or Path(str(rotated) + ".gz").exists():
        rotated = path.with_name(f"{path.name}.{stamp}.{counter}")
        counter += 1
    path.rename(rotated)
    # Avoid ``Path.with_suffix`` here: the timestamp segment looks like a suffix
    # to pathlib and would be clobbered rather than preserved.
    gz_path = Path(str(rotated) + ".gz")
    with open(rotated, "rb") as src, gzip.open(gz_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    rotated.unlink()


def _prune_old_rotations(directory: Path, stem: str) -> None:
    """Delete rotated files matching {stem}.* older than LOG_RETAIN_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOG_RETAIN_DAYS)
    for p in directory.glob(f"{stem}.*"):
        # Skip the live log itself (no timestamp suffix) and *.tmp partials.
        if p.name == stem or p.name.endswith(".tmp"):
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                p.unlink()
            except OSError as exc:
                print(f"[roadrunner] prune failed for {p}: {exc}", file=sys.stderr)


def rotate_logs() -> None:
    """Rotate oversized logs and prune old rotations. Called at task boundaries."""
    try:
        for path in (TRACE_LOG, CHANGELOG):
            _rotate_one(path)
        _prune_old_rotations(LOGS_DIR, TRACE_LOG.name)
        _prune_old_rotations(LOGS_DIR, CHANGELOG.name)
    except Exception as exc:
        # Rotation failures must never break the loop.
        print(f"[roadrunner] rotate_logs failed: {exc}", file=sys.stderr)


WORK_LOG_MARKER = "<!-- roadrunner: auto-generated below this line; edit ABOVE only -->"


def _build_work_log_auto_block(task: Task, validation_results: list[ValidationResult], notes: str) -> str:
    """Render the deterministic portion of a work log: completion timestamp,
    goal, acceptance criteria, validation transcript, notes.

    Everything this function returns is auto-generated from task metadata +
    validation output. Hand-authored prose (design rationale, decisions,
    follow-ups) belongs ABOVE the WORK_LOG_MARKER in the work log file and
    is preserved by write_work_log across repeated `complete` invocations.
    """
    passed_count = sum(1 for r in validation_results if r["passed"])
    total = len(validation_results)

    lines = [
        f"**Completed:** {_now()}",
        f"**Status:** {task.get('status')}",
        "",
        f"## Goal\n{task.get('goal', 'N/A')}",
        "",
        "## Acceptance Criteria",
    ]
    for ac in task.get("acceptance_criteria", []):
        lines.append(f"- {ac}")

    if validation_results:
        lines += ["", f"## Validation ({passed_count}/{total} passed)"]
        for r in validation_results:
            icon = "✅" if r["passed"] else "❌"
            lines.append(f"\n### {icon} `{r['command']}`")
            if not r["passed"] and _kind_note(r):
                lines.append(f"**{r['kind']}:** {_kind_note(r)}")
            if r["stdout"]:
                lines.append(f"```\n{r['stdout']}\n```")
            if r["stderr"] and not r["passed"]:
                lines.append(f"**stderr:**\n```\n{r['stderr']}\n```")

    if notes:
        lines += ["", f"## Notes\n{notes}"]

    return "\n".join(lines)


def write_work_log(task: Task, validation_results: list[ValidationResult], notes: str = "") -> None:
    """Write the per-task work log at ``logs/{task_id}.md``.

    Hand-authored prose (the agent wrote a narrative before running
    ``complete``) is preserved. The auto-generated portion — completion
    timestamp, goal, acceptance criteria, validation transcript, notes —
    is written below a stable marker line so repeated ``complete`` /
    ``block`` invocations replace only that section, idempotently.

    Two modes:
      * **File does not exist (or is empty):** write a fresh canonical
        template — H1 title + marker + auto block.
      * **File already exists with content:** preserve every line ABOVE
        the marker (or the entire file, if no marker is present yet),
        then append (or replace) the auto block below the marker. First
        invocation against a hand-authored file appends the marker; second
        and later invocations replace from the marker forward.
    """
    log_path = LOGS_DIR / f"{task['id']}.md"
    auto_block = _build_work_log_auto_block(task, validation_results, notes)

    if log_path.exists() and log_path.read_text().strip():
        existing = log_path.read_text()
        if WORK_LOG_MARKER in existing:
            # Idempotent replace: keep everything before the marker, drop
            # everything from the marker onward, re-emit the marker + a
            # fresh auto block. Lets the agent run `validate`→`complete`
            # repeatedly without piling up stale validation transcripts.
            head = existing.split(WORK_LOG_MARKER)[0].rstrip()
        else:
            # First time writing into a hand-authored file. Preserve the
            # entire existing content as prose, then add the marker +
            # auto block as a new appended section.
            head = existing.rstrip()
        log_path.write_text(head + "\n\n" + WORK_LOG_MARKER + "\n\n" + auto_block + "\n")
    else:
        # No prior file — write the canonical template. The H1 lives ABOVE
        # the marker so a future hand-edit of the title is preserved across
        # subsequent complete/block runs.
        header = f"# Work Log: {task['id']} — {task.get('title', '')}\n"
        log_path.write_text(header + "\n" + WORK_LOG_MARKER + "\n\n" + auto_block + "\n")


def write_reset_marker(task_id: str, summary: str) -> None:
    marker = ROOT / f".reset_{task_id}"
    marker.write_text(
        json.dumps(
            {"task_id": task_id, "summary": summary, "at": _now()},
            indent=2,
            ensure_ascii=False,
        )
    )


def write_context_snapshot() -> None:
    """Called by PreCompact hook — persist enough state for cold resume."""
    tasks = load_tasks()
    state = read_state()
    next_task = next_eligible_task(tasks)

    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_at": _now(),
        "current_task": state.get("current_task_id"),
        "iteration": state.get("iteration", 0),
        "next_eligible": next_task["id"] if next_task else None,
        "status_summary": {t["id"]: t["status"] for t in tasks},
    }
    (ROOT / ".context_snapshot.json").write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))


# ── CLI commands ──────────────────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> None:
    tasks = load_tasks()
    print(f"\n{'ID':<14} {'STATUS':<12} {'TITLE'}")
    print("─" * 60)
    for t in tasks:
        marker = "→" if t.get("status") == "in_progress" else " "
        # ROAD-012: append the per-task model hint inline so a quick scan shows
        # downshifted/upshifted tasks without changing the column layout.
        model = t.get("model")
        title_suffix = f"  [{model}]" if model else ""
        print(f"{marker} {t['id']:<13} {t.get('status', 'todo'):<12} {t.get('title', '')}{title_suffix}")
    next_t = next_eligible_task(tasks)
    print(f"\nNext eligible: {next_t['id'] if next_t else 'None'}")

    # ROAD-010: show both iteration counters so the operator can see which
    # value gates the runaway cap vs. which is lifetime audit.
    state = read_state()
    lifetime_iter = state.get("iteration", 0)
    session_iter = state.get("session_iteration", 0)
    print(f"Iteration (session): {session_iter}")
    print(f"Iteration (lifetime): {lifetime_iter}")

    # ROAD-011: session spend and configured budget. Always print spend so a
    # zero baseline is visible; budget line is suppressed when unset to keep
    # the default output uncluttered.
    session_cost = float(state.get("session_cost_usd", 0.0))
    budget = _resolve_max_budget_usd(None)
    if budget is not None:
        print(f"Session cost: ${session_cost:.2f} / ${budget:.2f} budget")
    else:
        print(f"Session cost: ${session_cost:.2f}")

    # ROAD-013: count of operator-learnings entries. Even on a fresh install
    # the line shows up as "Learnings: 0 entries" so operators see the
    # affordance exists and know where to append.
    learnings_count = len(_learnings_entries())
    print(f"Learnings: {learnings_count} entries ({LEARNINGS_FILE.name})")

    # ROAD-014: surface the last Claude Code session ID so the operator can
    # see at a glance whether `roadrunner resume --session-id` has a target.
    # Omitted entirely when not yet captured to keep fresh-install output clean.
    last_session_id = state.get("last_session_id")
    if isinstance(last_session_id, str) and last_session_id:
        print(f"Last Claude session: {last_session_id}")


def cmd_next(args: argparse.Namespace) -> None:
    tasks = load_tasks()
    task = next_eligible_task(tasks)
    if not task:
        print("No eligible tasks.")
        return
    print(f"\nNext: {task['id']} — {task.get('title')}")
    # ROAD-012: surface the per-task model hint above the goal so the operator
    # sees it before scrolling through criteria.
    model = task.get("model")
    if model:
        print(f"Model: {model}")
    print(f"Goal: {task.get('goal', 'N/A')}")
    print("Acceptance criteria:")
    for ac in task.get("acceptance_criteria", []):
        print(f"  - {ac}")


def cmd_start(args: argparse.Namespace) -> None:
    tasks = load_tasks()
    task = get_task(tasks, args.task_id)
    if not task:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)
    if not is_eligible(task, tasks):
        print(f"Task {args.task_id} is not eligible (status={task.get('status')}, check deps).")
        sys.exit(1)

    if active_task(tasks):
        raise ValueError("An active task already exists")
    state = read_state()
    attempts = state.get("attempts_per_task", {}).copy()
    attempts[args.task_id] = 0
    # Record base branch before creating task branch. Prefer the configured
    # project_base from tasks.yaml over _current_branch() to avoid stacking
    # task branches on top of previous task branches (ROAD-025).
    base_branch = get_project_base()
    branched = create_task_branch(args.task_id, base_branch=base_branch)
    if _is_git_repo() and not branched:
        raise ValueError("Task branch preparation failed; task was not started")
    task["status"] = "in_progress"
    save_tasks(tasks)
    write_state(
        args.task_id,
        state.get("iteration", 0),
        attempts,
        extra={"base_branch": base_branch},
    )
    append_changelog(args.task_id, "in_progress")
    trace_event(
        "task_start",
        task_id=args.task_id,
        iteration=state.get("iteration", 0),
        extra={"base_branch": base_branch, "branched": branched},
    )
    branch_msg = f" (branch: {task_branch_name(args.task_id)})" if branched else ""
    print(f"Started {args.task_id}. Iteration {state.get('iteration', 0)}.{branch_msg}")


def _failure_summary(results: list[ValidationResult], keep: int = 600) -> str:
    """One-paragraph description of the first failing result, for the retry brief (issue #21)."""
    for r in results:
        if r.get("passed"):
            continue
        out = (r.get("stderr") or "").strip() or (r.get("stdout") or "").strip()
        out = out[-keep:]
        head = f"{r.get('kind', 'fail')} in `{r.get('command', '?')}` (exit {r.get('returncode', '?')})"
        return f"{head}:\n{out}" if out else head
    return ""


def _record_failure_signature(task_id: str, passed: bool, results: list[ValidationResult]) -> None:
    """Persist the failure/diff signature (issue #19). Cleared on success."""
    from roadrunner import failure_sig as fs

    state = read_state()
    if passed:
        record = None
    else:
        prev = state.get("last_failure")
        record = fs.update_record(prev, task_id, fs.failure_sig(results), fs.diff_sig(ROOT))
        record["summary"] = _failure_summary(results)
    if record == state.get("last_failure"):
        return
    write_state(
        state.get("current_task_id"),
        state.get("iteration", 0),
        state.get("attempts_per_task"),
        extra={"last_failure": record},
    )
    if record and record["repeats"]:
        trace_event("repeat_failure", task_id=task_id, extra=record)


def cmd_validate(args: argparse.Namespace) -> None:
    tasks = load_tasks()
    task = get_task(tasks, args.task_id)
    if not task:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)

    trace_event("validate_start", task_id=args.task_id)
    passed, results = run_validation(task)
    _record_failure_signature(args.task_id, passed, results)
    # ROAD-015: split results into baseline/task suites so the operator can
    # see at a glance which phase failed. Baseline failures are gate failures
    # (CI parity violation); task-specific failures are scope-specific to the
    # task. Same severity for exit code, different message.
    baseline_results = [r for r in results if r.get("phase") == "baseline"]
    task_results = [r for r in results if r.get("phase") == "task"]
    baseline_passed = all(r["passed"] for r in baseline_results)

    def _render(label: str, rows: list[ValidationResult]) -> None:
        if not rows:
            return
        print(f"── {label} ──")
        for r in rows:
            icon = "✅" if r["passed"] else "❌"
            print(f"{icon} {r['command']}")
            if not r["passed"] and _kind_note(r):
                print(f"   [{r['kind']}] {_kind_note(r)}")
            if not r["passed"] and r["stderr"]:
                print(f"   {r['stderr'][:200]}")

    _render("baseline", baseline_results)
    if baseline_results and not baseline_passed:
        print("Baseline failed — task-specific commands were not run.")
    _render("task", task_results)

    trace_event(
        "validate_end",
        task_id=args.task_id,
        extra={
            "passed": passed,
            "total": len(results),
            "baseline_total": len(baseline_results),
            "task_total": len(task_results),
        },
    )
    sys.exit(0 if passed else 1)


def cmd_complete(args: argparse.Namespace) -> None:
    tasks = load_tasks()
    task = get_task(tasks, args.task_id)
    if not task:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)

    state = read_state()
    if task.get("status") != "in_progress" or state.get("current_task_id") != args.task_id:
        raise ValueError("Completion requires the current active task")
    if any(not (prerequisite := get_task(tasks, dep)) or prerequisite.get("status") != "done"
           for dep in task.get("depends_on", [])):
        raise ValueError("Completion requires resolved dependencies")
    if not task.get("validation_commands") and not get_baseline_validation():
        raise ValueError("Completion requires validation commands")
    passed, results = run_validation(task)
    _record_failure_signature(args.task_id, passed, results)
    if not passed:
        print(f"Validation failed. Task {args.task_id} NOT marked done.")
        write_work_log(task, results, notes=args.notes or "")
        sys.exit(1)

    base_branch = state.get("base_branch", "main")
    merged = merge_task_branch(args.task_id, base_branch)
    if not merged:
        raise ValueError("Integration failed; task remains unfinished. Repair the task branch and retry.")
    task["status"] = "done"
    save_tasks(tasks)
    write_work_log(task, results, notes=args.notes or "")
    append_changelog(args.task_id, "done", notes=args.notes or "")
    write_reset_marker(args.task_id, summary=args.notes or "Completed.")
    state = read_state()
    # Merge task branch back to base if it exists
    base_branch = state.get("base_branch", "main")
    # Clear current_task_id so SessionStart / check_stop don't read a stale
    # "resume this done task" pointer on the next fire. Preserve iteration,
    # attempts, and base_branch for continuity.
    write_state(
        None,
        state.get("iteration", 0),
        state.get("attempts_per_task", {}),
        extra={"base_branch": base_branch} if base_branch else None,
    )
    trace_event(
        "task_complete",
        task_id=args.task_id,
        iteration=state.get("iteration"),
        extra={"merged": merged},
    )
    print(f"✅ {args.task_id} marked done.")


def cmd_block(args: argparse.Namespace) -> None:
    tasks = load_tasks()
    task = get_task(tasks, args.task_id)
    if not task:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)

    task["status"] = "blocked"
    save_tasks(tasks)
    append_changelog(args.task_id, "blocked", notes=args.notes or "")
    write_work_log(task, [], notes=args.notes or "")
    state = read_state()
    trace_event(
        "task_block",
        task_id=args.task_id,
        iteration=state.get("iteration"),
        extra={"notes": args.notes or ""},
    )
    print(f"🚫 {args.task_id} marked blocked.")


def cmd_reset(args: argparse.Namespace) -> None:
    write_reset_marker(args.task_id, summary=args.summary or "")
    write_context_snapshot()
    rotate_logs()
    print(f"Reset marker written for {args.task_id}.")


def cmd_reset_iteration(args: argparse.Namespace) -> None:
    """ROAD-010: explicit user control over the iteration counters.

      --soft  zeros session_iteration only. Use after a runaway cap fire to
              start a fresh session window without losing the lifetime audit
              trail.

      --hard  zeros both iteration (lifetime) and session_iteration. Use only
              when fully restarting the roadmap (e.g. after a failed experiment
              that you want to erase from the trace entirely). Destructive to
              the audit counter.

    Defaults to --soft if neither flag is given. The two flags are mutually
    exclusive; hard wins if both are passed (argparse already warns, but we
    guard explicitly for clarity).
    """
    hard = bool(getattr(args, "hard", False))
    # --soft is the default; --hard is the explicit opt-in to nuke lifetime
    with _exclusive_state_lock():
        state = read_state()
        new_lifetime = 0 if hard else state.get("iteration", 0)
        write_state(
            state.get("current_task_id"),
            new_lifetime,
            state.get("attempts_per_task"),
            session_iteration=0,
            # ROAD-011: cost is per-session, so a soft reset (which the operator
            # uses to start a fresh session window after a cap fire) must clear
            # it too — otherwise the new session begins already over budget.
            session_cost_usd=0.0,
        )
    trace_event(
        "reset_iteration",
        task_id=state.get("current_task_id"),
        iteration=new_lifetime,
        extra={
            "mode": "hard" if hard else "soft",
            "session_iteration": 0,
            "previous_lifetime": state.get("iteration", 0),
        },
    )
    mode = "hard" if hard else "soft"
    if hard:
        print(f"reset-iteration ({mode}): session=0, lifetime=0 (was {state.get('iteration', 0)})")
    else:
        print(f"reset-iteration ({mode}): session=0, lifetime={new_lifetime} (preserved)")


# ── Scope-aware commit (ROAD-021) ─────────────────────────────────────────────

# Files outside a task's `files_expected` that are still legitimately part of a
# task commit. These are the artifacts roadrunner itself writes: status updates,
# work logs, boundary markers, backup rotations.
_COMMIT_OVERLAY_PREFIXES: tuple[str, ...] = (
    "logs/",
    "tasks/tasks.yaml",  # covers tasks.yaml, tasks.yaml.bak, tasks.yaml.bak.N
    ".reset_",  # any task's boundary marker — audit trail
)

_VALID_COMMIT_TYPES: frozenset[str] = frozenset({"feat", "fix", "chore", "docs", "refactor", "test", "perf", "style", "ci", "build"})


def _parse_porcelain(output: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain` output into (status_code, path) tuples.

    For renames (`R  old -> new`), returns the new path — that's what we want
    to stage. Lines shorter than 3 chars are skipped (shouldn't happen but
    defensive against edge cases).
    """
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        if len(line) < 3:
            continue
        status = line[:2]
        rest = line[3:]
        if " -> " in rest:
            _, path = rest.split(" -> ", 1)
        else:
            path = rest
        entries.append((status, path))
    return entries


def _is_in_commit_scope(path: str, files_expected: set[str]) -> bool:
    """A path belongs in a task commit if it's in files_expected OR matches
    the roadrunner overlay (logs/, tasks.yaml*, .reset_*)."""
    if path in files_expected:
        return True
    return any(path.startswith(prefix) for prefix in _COMMIT_OVERLAY_PREFIXES)


def cmd_commit(args: argparse.Namespace) -> None:
    """Scope-aware commit wrapper. Stages only files in the task's declared
    `files_expected` plus the roadrunner overlay, refusing if anything out of
    scope is dirty. Replaces the unsafe `git add -A && git commit` pattern that
    sweeps unrelated changes into task commits (observed in the entra-triage
    pilot, b21c768)."""
    tasks = load_tasks()
    task = get_task(tasks, args.task_id)
    if task is None:
        print(f"Task {args.task_id} not found in tasks.yaml.", file=sys.stderr)
        sys.exit(1)

    if not _is_git_repo():
        print("Not a git repository; nothing to commit.", file=sys.stderr)
        sys.exit(1)

    commit_type = args.type or "feat"
    if commit_type not in _VALID_COMMIT_TYPES:
        print(
            f"Invalid --type {commit_type!r}. Must be one of: {', '.join(sorted(_VALID_COMMIT_TYPES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    status_out = _git("status", "--porcelain", check=False)
    if status_out.returncode != 0:
        print(f"git status failed: {status_out.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    files_expected = set(task.get("files_expected") or [])
    in_scope: list[str] = []
    out_of_scope: list[str] = []
    for _status, path in _parse_porcelain(status_out.stdout):
        if _is_in_commit_scope(path, files_expected):
            in_scope.append(path)
        else:
            out_of_scope.append(path)

    if out_of_scope:
        print(f"❌ Refusing to commit {args.task_id}: out-of-scope files are dirty.\n", file=sys.stderr)
        print("These files are not in the task's files_expected and not part of the", file=sys.stderr)
        print("roadrunner overlay (logs/, tasks/tasks.yaml*, .reset_*):\n", file=sys.stderr)
        for path in out_of_scope:
            print(f"  {path}", file=sys.stderr)
        print(
            "\nResolve one of three ways:\n"
            "  (a) add them to the task's files_expected in tasks/tasks.yaml\n"
            "  (b) git stash them — they belong in a separate commit\n"
            "  (c) git checkout -- <path> to discard (destructive)\n",
            file=sys.stderr,
        )
        sys.exit(1)

    if not in_scope:
        print(f"Nothing to commit for {args.task_id} (working tree clean).")
        return

    add_result = _git("add", "--", *in_scope, check=False)
    if add_result.returncode != 0:
        print(f"git add failed: {add_result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    title = str(task.get("title") or args.task_id).strip()
    subject = f"{commit_type}({args.task_id}): {title}"
    message = subject if not args.notes else f"{subject}\n\n{args.notes}"

    commit_result = _git("commit", "-m", message, check=False)
    if commit_result.returncode != 0:
        print(f"git commit failed: {commit_result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    trace_event(
        "task_commit",
        task_id=args.task_id,
        extra={"files": in_scope, "type": commit_type},
    )
    print(f"✅ Committed {args.task_id}: {subject}")
    print(f"   {len(in_scope)} file(s) staged + committed")


def cmd_pause(args: argparse.Namespace) -> None:
    (ROOT / ".roadrunner_paused").touch()
    print("Roadrunner paused. Stop hook will exit 0 until resumed.")


def cmd_resume(args: argparse.Namespace) -> None:
    # ROAD-014: overloaded subcommand. The verb "resume" always resumes the
    # loop — the pause marker is cleared on every invocation regardless of
    # flag. Sub-modes layer extra behavior:
    #
    # Default (no flags): just clear the marker + print confirmation.
    # --session-id: also print the `claude --resume <id>` command for the
    #   operator to paste, using the last_session_id captured by SessionStart.
    # --exec: also exec `claude --resume <id>` in place. Useful when scripting
    #   recovery from a crash.
    want_session_id = bool(getattr(args, "session_id", False))
    want_exec = bool(getattr(args, "exec_", False))

    # Clear the pause marker FIRST, before the flag branches can early-return
    # or exec away. An operator who paused for ad-hoc work and then runs
    # `resume --session-id` (or `--exec`) intends to resume; not unpausing
    # leaves them silently dead-lined when they reattach to Claude.
    paused_marker = ROOT / ".roadrunner_paused"
    was_paused = paused_marker.exists()
    paused_marker.unlink(missing_ok=True)

    if want_session_id or want_exec:
        state = read_state()
        sid = state.get("last_session_id")
        if not isinstance(sid, str) or not sid:
            print(
                "No Claude Code session ID has been recorded yet. "
                "Start a session under the SessionStart hook so the ID is "
                "captured into .roadmap_state.json, then re-run "
                "`roadrunner resume --session-id`.",
                file=sys.stderr,
            )
            sys.exit(1)
        if was_paused:
            # Surface the side effect so operators who flipped pause earlier
            # know it's been cleared by this invocation.
            print("Roadrunner resumed (pause marker cleared).", file=sys.stderr)
        if want_exec:
            try:
                os.execvp("claude", ["claude", "--resume", sid])
            except OSError as exc:
                print(
                    f"failed to exec `claude --resume {sid}`: {exc}",
                    file=sys.stderr,
                )
                sys.exit(1)
        print(f"claude --resume {sid}")
        return

    print("Roadrunner resumed. Loop active.")


def cmd_health(args: argparse.Namespace) -> None:
    if (ROOT / ".roadrunner_paused").exists():
        print("⚠️  PAUSED — .roadrunner_paused present. Run `roadrunner resume` to re-engage loop.")
    tasks = load_tasks()
    eligible = [t for t in tasks if is_eligible(t, tasks)]
    done = [t for t in tasks if t.get("status") == "done"]
    blocked = [t for t in tasks if t.get("status") == "blocked"]
    print(f"healthy — {len(done)}/{len(tasks)} done, {len(eligible)} eligible, {len(blocked)} blocked")
    last_session_line = rr_session.health_line()
    if last_session_line:
        print(last_session_line)

    # ROAD-011: surface session spend + configured budget in the health summary
    # so an operator running `roadrunner health` overnight can see the budget
    # status alongside iteration/eligibility counts.
    state = read_state()
    session_cost = float(state.get("session_cost_usd", 0.0))
    budget = _resolve_max_budget_usd(None)
    if budget is not None:
        print(f"budget: ${session_cost:.2f} / ${budget:.2f}")
    elif session_cost > 0:
        print(f"budget: ${session_cost:.2f} (no cap set)")


def cmd_sessions(args: argparse.Namespace) -> None:
    """Pretty-print recent finalized session summaries, newest-first."""
    limit = int(args.last) if args.last else 10
    summaries = rr_session.list_sessions(limit=limit)
    if not summaries:
        print("No finalized sessions yet.")
        return
    blocks = [rr_session.format_session(s) for s in summaries]
    print("\n\n".join(blocks))


# ── watch: live read-only monitor ─────────────────────────────────────────────


def _tail_trace_events(n: int = 5) -> list[dict]:
    """Return the last ``n`` parseable JSONL records from ``TRACE_LOG``.

    Tolerates a missing file, an empty file, and partial trailing lines from
    a concurrent in-flight write — bad lines are silently skipped so the
    watch loop never crashes on a torn append.
    """
    if not TRACE_LOG.exists():
        return []
    try:
        with open(TRACE_LOG) as f:
            lines = list(deque(f, maxlen=n))
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _trace_start_ts() -> datetime | None:
    """Return the timestamp of the first trace event, or None if unavailable."""
    if not TRACE_LOG.exists():
        return None
    try:
        with open(TRACE_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts") if isinstance(rec, dict) else None
                if isinstance(ts, str):
                    try:
                        return datetime.fromisoformat(ts)
                    except ValueError:
                        return None
                return None
    except OSError:
        return None
    return None


def _format_elapsed(start: datetime | None, now: datetime) -> str:
    if start is None:
        return "—"
    delta = now - start
    total = int(delta.total_seconds())
    if total < 0:
        return "—"
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _render_watch_frame(max_iter: int, now: datetime | None = None) -> str:
    """Compose one watch frame as a string. Pure function — all IO is best-effort
    and exceptions render a single error line instead of propagating."""
    moment = now or datetime.now(timezone.utc)
    header_ts = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    lines.append(f"─ roadrunner watch ─────────────────── {header_ts} ─")
    lines.append("")

    try:
        state = read_state()
    except SystemExit:
        # read_state() exits on forward-incompat schema; surface and continue.
        lines.append(" state: unreadable (schema gate)")
        state = {}
    session_iter = state.get("session_iteration", 0) if isinstance(state, dict) else 0
    lifetime_iter = state.get("iteration", 0) if isinstance(state, dict) else 0
    attempts_map = state.get("attempts_per_task", {}) if isinstance(state, dict) else {}

    lines.append(f" Iteration:   session {session_iter} / {max_iter}     lifetime {lifetime_iter}")
    lines.append(f" Elapsed:     {_format_elapsed(_trace_start_ts(), moment)}  (since first trace event)")

    try:
        tasks = load_tasks()
        tasks_err: str | None = None
    except (FileNotFoundError, ValueError, OSError) as exc:
        tasks = []
        tasks_err = str(exc)

    if tasks_err:
        lines.append(f" tasks.yaml:  unreadable — {tasks_err}")
    else:
        active = active_task(tasks)
        if active is not None:
            attempts = attempts_map.get(active["id"], 0) if isinstance(attempts_map, dict) else 0
            lines.append(f" Active:      {active['id']}  attempt {attempts}/{MAX_TASK_ATTEMPTS}  — {active.get('title', '')}")
        else:
            lines.append(" Active:      (no active task)")

        nxt = next_eligible_task(tasks)
        lines.append(f" Next up:     {nxt['id'] if nxt else '(none eligible)'}")

        counts = {"done": 0, "in_progress": 0, "todo": 0, "blocked": 0}
        for t in tasks:
            counts[t.get("status", "todo")] = counts.get(t.get("status", "todo"), 0) + 1
        lines.append("")
        lines.append(
            f" Status:      done {counts['done']}   in_progress {counts['in_progress']}   "
            f"todo {counts['todo']}   blocked {counts['blocked']}"
        )

    lines.append("")
    lines.append(" Recent events (last 5):")
    events = _tail_trace_events(5)
    if not events:
        lines.append("   (no trace events yet)")
    else:
        for rec in events:
            ts_raw = rec.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_raw).strftime("%H:%M:%SZ")
            except (TypeError, ValueError):
                ts = "--:--:--"
            event = str(rec.get("event", "?"))[:22]
            tid = rec.get("task_id") or "—"
            lines.append(f"   {ts}  {event:<22} {tid}")

    lines.append("")
    lines.append(" (Ctrl-C to exit)")
    lines.append("")
    return "\n".join(lines)


def cmd_watch(args: argparse.Namespace) -> None:
    """Live read-only monitor. Polls disk state on a fixed interval and
    redraws a status frame. Exits 0 on Ctrl-C. Never writes state."""
    interval = max(0.5, float(args.interval))  # floor: --interval 0 must not busy-loop
    max_iter_env = os.environ.get("ROADMAP_MAX_ITERATIONS")
    try:
        max_iter = int(max_iter_env) if max_iter_env else 100
    except ValueError:
        max_iter = 100

    try:
        while True:
            try:
                frame = _render_watch_frame(max_iter=max_iter)
            except Exception as exc:  # observability tool; never crash the loop
                frame = f"watch: render failed: {exc}\n"
            sys.stdout.write("\x1b[2J\x1b[H")  # clear screen + cursor home
            sys.stdout.write(frame)
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return


def cmd_check_stop(args: argparse.Namespace) -> None:
    """
    Called by Stop hook. Outputs JSON to control whether Claude Code halts.
    Reads stop_hook_active from stdin to prevent infinite loops.
    """
    try:
        stdin_data = json.loads(sys.stdin.read())
    except Exception:
        stdin_data = {}

    # stop_hook_active is Claude Code's signal that a previous hook invocation
    # in this session already blocked-and-continued. Historically we early-exited
    # on this to avoid infinite loops, but that killed legitimate multi-task
    # runs the moment the flag flipped true — the loop would stop after the
    # first task boundary and the user would have to restart the session to
    # move on. Treat the flag as a hint: only allow stop if the roadmap is
    # genuinely finished (no active task, no eligible next task). Otherwise
    # fall through to normal decision logic — the iteration cap and per-task
    # auto-block (5 attempts) are the real safety nets against runaway loops.
    hook_looping = bool(stdin_data.get("stop_hook_active"))

    def _finalize_session_quiet() -> None:
        try:
            rr_session.finalize_current()
        except OSError as exc:
            print(f"[roadrunner] session finalize failed: {exc}", file=sys.stderr)

    # Serialize concurrent Stop-hook fires so the read→increment→write span in the
    # body below is atomic. SystemExit from any sys.exit() still releases the lock
    # via the context manager's finally.
    with _exclusive_state_lock():
        state = read_state()
        # ROAD-010: two counters. `iteration` is lifetime-cumulative (audit trail,
        # preserved across sessions); `session_iteration` resets on every
        # SessionStart hook and is the one gated by --max-iterations. Prior to
        # this change the lifetime counter was the cap — which meant that once
        # it crossed the cap, every subsequent `claude` invocation halted on
        # its first Stop fire. The cap is a runaway-protection primitive, not
        # a project-lifetime ceiling.
        iteration = state.get("iteration", 0) + 1
        session_iteration = state.get("session_iteration", 0) + 1
        attempts = state.get("attempts_per_task", {})
        max_iter = int(args.max_iterations) if args.max_iterations else 100
        max_attempts = int(args.max_attempts) if args.max_attempts else MAX_TASK_ATTEMPTS

        # ROAD-011: pull per-session cost out of the Stop-hook payload. If the
        # field isn't there, preserve the previously-persisted value rather
        # than zeroing it — Claude Code's payload schema isn't stable yet, so a
        # missing field on one fire is not a signal to forget what we knew on
        # the prior fire. cmd_session_start is the only place that resets to 0.
        observed_cost = _extract_session_cost_usd(stdin_data)
        previous_cost = float(state.get("session_cost_usd", 0.0))
        session_cost_usd = observed_cost if observed_cost is not None else previous_cost
        max_budget_usd = _resolve_max_budget_usd(getattr(args, "max_budget_usd", None))

        # Emit the "no cost data available" warning exactly once per process
        # when an operator has configured a budget but Claude Code isn't
        # surfacing the field we look for. The feature still no-ops in this
        # case so the loop keeps running — failing closed on a missing payload
        # field would be more annoying than the cost overrun the budget is
        # meant to catch.
        global _cost_data_warning_emitted
        if max_budget_usd is not None and observed_cost is None and previous_cost == 0.0 and not _cost_data_warning_emitted:
            print(
                "[roadrunner] ROADMAP_MAX_BUDGET_USD is set but no cost data was "
                "found in the Stop-hook payload; budget halt is a no-op for this "
                "session. Upgrade Claude Code or unset the budget to silence.",
                file=sys.stderr,
            )
            _cost_data_warning_emitted = True

        write_state(
            state.get("current_task_id"),
            iteration,
            attempts,
            session_iteration=session_iteration,
            session_cost_usd=session_cost_usd,
        )
        # ROAD-012: include the active task's model hint in every check_stop
        # event so trace.jsonl readers can correlate model choice with
        # outcomes (auto-block rate, iteration spend, cost spend). Resolved
        # here rather than from state because the field lives on the task,
        # not on session state.
        active_task_id = state.get("current_task_id")
        active_model: str | None = None
        if active_task_id:
            try:
                tasks_for_trace = load_tasks()
                active = get_task(tasks_for_trace, active_task_id)
                if active:
                    active_model = active.get("model")
            except (OSError, ValueError):
                # Tasks file unreadable mid-loop is already handled later;
                # the trace just omits the field rather than crashing.
                pass
        trace_event(
            "check_stop",
            task_id=active_task_id,
            iteration=iteration,
            extra={
                "max_iter": max_iter,
                "session_iteration": session_iteration,
                "session_cost_usd": round(session_cost_usd, 4),
                "max_budget_usd": max_budget_usd,
                "model": active_model,
            },
        )

        # ROAD-011: budget halt fires before the iteration cap so an over-spend
        # is reported with the right stopReason. Only triggers when cost data
        # has actually been populated (>0) — a configured budget against a 0.0
        # baseline would fire spuriously on the first fire.
        if max_budget_usd is not None and session_cost_usd >= max_budget_usd and session_cost_usd > 0:
            trace_event(
                "budget_exceeded",
                task_id=state.get("current_task_id"),
                iteration=iteration,
                extra={
                    "session_cost_usd": round(session_cost_usd, 4),
                    "max_budget_usd": max_budget_usd,
                },
            )
            _finalize_session_quiet()
            print(
                json.dumps(
                    {
                        "continue": False,
                        "stopReason": (
                            f"Budget cap (${max_budget_usd:.2f}) reached for this "
                            f"session: spent ${session_cost_usd:.2f}. Roadmap loop "
                            f"halted. Raise ROADMAP_MAX_BUDGET_USD or run "
                            f"'roadrunner reset-iteration --soft' to start a fresh "
                            f"session window."
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            sys.exit(0)

        if session_iteration >= max_iter:
            _finalize_session_quiet()
            print(
                json.dumps(
                    {
                        "continue": False,
                        "stopReason": (
                            f"Max iterations ({max_iter}) reached for this session "
                            f"(lifetime: {iteration}). Roadmap loop halted. "
                            f"Use 'roadrunner reset-iteration --soft' to start a fresh "
                            f"session window."
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            sys.exit(0)

        try:
            tasks = load_tasks()
        except (ValueError, OSError) as exc:
            # A broken queue cannot drive decisions. Halt in a controlled way: structured
            # JSON, exit 0, an actionable instruction, and never a completion record.
            _finalize_session_quiet()
            print(
                json.dumps(
                    {
                        "continue": False,
                        "stopReason": (
                            f"Roadmap loop halted: the task queue cannot be read ({exc}). "
                            "Restore tasks/tasks.yaml from tasks/tasks.yaml.bak or version control, "
                            "or fix the error above, then resume the session."
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            sys.exit(0)
        last_msg = stdin_data.get("last_assistant_message", "")

        # If Claude Code flagged this fire as a potential hook loop AND there is
        # genuinely nothing left to do (no active task, no eligible next task),
        # allow the stop. Otherwise we ignore the flag and keep driving — stalled
        # sessions still get caught by the iteration cap below.
        if hook_looping and not active_task(tasks) and not next_eligible_task(tasks):
            _finalize_session_quiet()
            sys.exit(0)

        # Completion signal: Claude outputs ROADMAP_COMPLETE as the last non-empty line
        if is_completion_signal(last_msg) and all(t.get("status") == "done" for t in tasks):
            append_changelog(
                "ALL",
                "complete",
                notes="Roadmap finished — ROADMAP_COMPLETE signal received.",
            )
            _finalize_session_quiet()
            sys.exit(0)

        # Resume in-progress task if Claude responded mid-task
        in_flight = active_task(tasks)
        if in_flight:
            task_attempts = increment_attempts(state, in_flight["id"])
            write_state(
                in_flight["id"],
                iteration,
                state["attempts_per_task"],
                session_iteration=session_iteration,
            )
            last_failure = state.get("last_failure") or {}
            repeated = last_failure.get("task_id") == in_flight["id"] and last_failure.get("repeats", 0) >= 1
            if task_attempts >= max_attempts or repeated:
                in_flight["status"] = "blocked"
                save_tasks(tasks)
                append_changelog(
                    in_flight["id"],
                    "blocked",
                    notes=(
                        f"Auto-blocked: identical failure and identical diff on consecutive validations (attempt {task_attempts})."
                        if repeated and task_attempts < max_attempts
                        else f"Auto-blocked after {task_attempts} attempts without completion."
                    ),
                )
                trace_event(
                    "auto_block",
                    task_id=in_flight["id"],
                    iteration=iteration,
                    extra={"attempts": task_attempts, "max_attempts": max_attempts, "repeat_failure": repeated},
                )
                msg = (
                    f"Task {in_flight['id']} auto-blocked after {task_attempts} attempts"
                    f"{' (same failure, same diff, no progress)' if repeated and task_attempts < max_attempts else ''}. "
                    f"Move to the next task or output ROADMAP_COMPLETE on its own line."
                )
                print(json.dumps({"decision": "block", "reason": msg}, ensure_ascii=False))
                sys.exit(0)
            brief = _build_task_brief(in_flight, session_iteration, max_iter, resume=True)
            print(json.dumps({"decision": "block", "reason": brief}, ensure_ascii=False))
            sys.exit(0)

        next_task = next_eligible_task(tasks)
        if next_task:
            brief = _build_task_brief(next_task, session_iteration, max_iter)
            print(json.dumps({"decision": "block", "reason": brief}, ensure_ascii=False))
            sys.exit(0)

        # No active or eligible task — check for blocked before declaring done
        blocked = [t["id"] for t in tasks if t.get("status") == "blocked"]
        if blocked:
            msg = f"No eligible tasks. Blocked: {blocked}. Investigate and unblock, or output ROADMAP_COMPLETE on its own line to halt."
            print(json.dumps({"decision": "block", "reason": msg}, ensure_ascii=False))
            sys.exit(0)

        remaining = [t["id"] for t in tasks if t.get("status") not in ("done",)]
        if remaining:
            msg = (
                f"No eligible tasks, but these are not done: {remaining}. "
                "Check dependencies and status. Output ROADMAP_COMPLETE on its own line only if roadmap is truly finished."
            )
            print(json.dumps({"decision": "block", "reason": msg}, ensure_ascii=False))
            sys.exit(0)

        msg = "All tasks complete. Output ROADMAP_COMPLETE on its own line to signal completion."
        print(json.dumps({"decision": "block", "reason": msg}, ensure_ascii=False))
        sys.exit(0)


def cmd_snapshot(args: argparse.Namespace) -> None:
    write_context_snapshot()


def cmd_post_compact(args: argparse.Namespace) -> None:
    """Called by the PostCompact hook after Claude Code completes compaction.

    Reads the optional hook payload (trigger, compact_summary) from stdin,
    then verifies that ``.context_snapshot.json`` is present, parseable, on
    the expected schema, and contains every required field. Outcome is
    written to ``trace.jsonl`` as a ``post_compact_verify`` event.

    PostCompact does not support decision control per the Claude Code hooks
    reference, so this command is purely informational and always exits 0
    — observability must never break the control loop.
    """
    required_fields = (
        "schema_version",
        "snapshot_at",
        "current_task",
        "iteration",
        "next_eligible",
        "status_summary",
    )

    trigger: str | None = None
    compact_summary: str | None = None
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    raw_trigger = payload.get("trigger")
                    if isinstance(raw_trigger, str):
                        trigger = raw_trigger
                    raw_summary = payload.get("compact_summary")
                    if isinstance(raw_summary, str):
                        compact_summary = raw_summary
    except (json.JSONDecodeError, OSError, ValueError):
        # Malformed stdin must not break the loop; the missing fields will
        # surface in the trace event below.
        pass

    snapshot_path = ROOT / ".context_snapshot.json"
    success = False
    missing: list[str] = []
    schema_observed: Any = None
    snapshot_data: dict[str, Any] = {}

    if not snapshot_path.exists():
        missing = ["<file-missing>"]
    else:
        try:
            parsed = json.loads(snapshot_path.read_text())
            if isinstance(parsed, dict):
                snapshot_data = parsed
                missing = [f for f in required_fields if f not in snapshot_data]
                schema_observed = snapshot_data.get("schema_version")
                if not missing and schema_observed == SNAPSHOT_SCHEMA_VERSION:
                    success = True
            else:
                missing = ["<not-an-object>"]
        except (json.JSONDecodeError, OSError):
            missing = ["<unreadable>"]

    extra: dict[str, Any] = {"success": success}
    if trigger is not None:
        extra["trigger"] = trigger
    if compact_summary is not None:
        # Bound the trace line — compaction summaries can be long.
        extra["compact_summary"] = compact_summary[:500]
    if missing:
        extra["missing_fields"] = missing
    if schema_observed is not None and schema_observed != SNAPSHOT_SCHEMA_VERSION:
        extra["schema_mismatch"] = {
            "expected": SNAPSHOT_SCHEMA_VERSION,
            "got": schema_observed,
        }
    if snapshot_data.get("snapshot_at"):
        extra["snapshot_at"] = snapshot_data["snapshot_at"]

    trace_event(
        "post_compact_verify",
        task_id=snapshot_data.get("current_task"),
        extra=extra,
    )


_INIT_TASKS_TEMPLATE = """\
# project_base: branch that task branches fork from (prevents stacking).
# Task branches auto-merge back into this on `roadrunner complete`.
project_base: main

# push_on_complete: what to push to origin after a successful merge.
#   base — push project_base (origin tracks your local main as tasks land)
#   task — push the task branch before delete (keeps per-task history remote)
#   both — push both
#   none — local-only (offline / no remote configured)
push_on_complete: base

tasks:
  - id: TASK-001
    title: "First task — replace me"
    status: todo
    depends_on: []
    goal: |
      Describe what this task accomplishes. Keep the scope to a single
      boundary — one task per cycle, no side quests.
    acceptance_criteria:
      - "Describe a concrete, observable outcome"
    validation_commands:
      - "echo 'replace with a real check (test, lint, build)'"
    files_expected: []
"""

_INIT_LEARNINGS_TEMPLATE = """\
# Operator Learnings

This file is the project's append-only log of non-obvious facts the agent
discovered the hard way — build commands that look wrong but are right,
flaky tests that need a retry, env vars that must be set before a script
will work, file paths that moved, and any other "I wish I'd known this an
hour ago" notes. Each entry is one line, dated, kept brief. The
SessionStart hook reads the last 20 lines and surfaces them to the next
session so the lessons stick across context boundaries. Append; do not
edit prior entries (history is the point).

<!-- Append new entries below. Format: `YYYY-MM-DD — terse fact`. -->
"""

_INIT_CLAUDE_MD_TEMPLATE = """\
# CLAUDE.md — Roadmap Loop Agent Brief

You are executing a deterministic roadmap. Python owns control. You own
implementation. One task per cycle. No side quests. No skipping ahead.

## Each cycle

1. `roadrunner next` — shows the next eligible task
2. `roadrunner start TASK-XXX` — creates `roadrunner/TASK-XXX` from
   the project base and switches to it
3. Implement, staying strictly inside the task's `files_expected`
4. `roadrunner validate TASK-XXX` — run every validation command;
   they must all exit 0
5. `roadrunner commit TASK-XXX --notes "..."` — stages only files
   in `files_expected` + roadrunner overlay (logs/, tasks.yaml, .reset_*) and
   commits with a conventional message. **Do not use `git add -A`** — it
   sweeps unrelated changes into your task commit. If the commit refuses,
   fix the out-of-scope files (stash, add to files_expected, or discard).
6. `roadrunner complete TASK-XXX --notes "what you did"` — validates
   the active task and requires successful Git integration before marking done
7. `roadrunner reset TASK-XXX --summary "one-line"` — writes the
   boundary marker for the next task

## Completion signal

When every task is `done` and `next` reports nothing eligible, output the
sentinel `ROADMAP_COMPLETE` on its own line as the last non-empty line of
your message. This halts the loop cleanly.

## Auto-block

If you resume the same task 5 times without completing it, the Stop hook will
auto-block it and move on. If you hit a real blocker earlier than that,
`roadrunner block TASK-XXX --notes "why"` is the explicit path.

## File scope

Only edit files listed in the current task's `files_expected` and
`documentation_targets`. If something outside scope needs changing, note it
in the task's `--notes` or open a follow-up task — don't silently expand
scope.

## Operator learnings (logs/learnings.md)

Whenever you discover a non-obvious project fact during a task — a build
command that's unusual, a flaky test that needs a retry, an env var the
script silently requires, a file that moved — append a one-line entry to
`logs/learnings.md` in the form `YYYY-MM-DD — terse fact`. The SessionStart
hook injects the last 20 entries into the next session's context so the
lesson persists across compaction and across `claude` restarts. Treat it
as append-only; never edit prior entries.
"""


def _init_plan(target: Path, source: Path) -> list[tuple[str, Path, Path | None, str | None]]:
    """Return the list of scaffold actions as (kind, dest, src_or_none, content_or_none).

    kind is one of: 'mkdir', 'write', 'copy'. For 'write', content is the string to
    emit; for 'copy', src is the source path on disk.
    """
    plan: list[tuple[str, Path, Path | None, str | None]] = [
        ("mkdir", target / "tasks", None, None),
        ("write", target / "tasks" / "tasks.yaml", None, _INIT_TASKS_TEMPLATE),
        ("mkdir", target / "logs", None, None),
        ("write", target / "logs" / ".gitkeep", None, ""),
        # ROAD-013: ship the operator learnings log on init so the affordance
        # is discoverable from day one. The header is the only content; the
        # agent appends real entries as it works.
        ("write", target / "logs" / "learnings.md", None, _INIT_LEARNINGS_TEMPLATE),
        ("write", target / "CLAUDE.md", None, _INIT_CLAUDE_MD_TEMPLATE),
    ]
    settings_src = source / ".claude" / "settings.json"
    if settings_src.is_file():
        plan.append(("mkdir", target / ".claude", None, None))
        plan.append(("copy", target / ".claude" / "settings.json", settings_src, None))
    hooks_src = source / "hooks"
    if hooks_src.is_dir():
        plan.append(("mkdir", target / "hooks", None, None))
        for hook_file in sorted(hooks_src.iterdir()):
            if hook_file.is_file():
                plan.append(("copy", target / "hooks" / hook_file.name, hook_file, None))
    return plan


def _find_template_source() -> Path:
    """Locate the directory shipping init scaffold templates (hooks/, .claude/).

    Climbs from ``src/roadrunner/cli.py`` to the package source root that
    sits next to ``hooks/`` and ``.claude/`` in a development checkout.
    For pip-installed wheels the returned path is ``site-packages`` and
    won't contain those template dirs; ``_init_plan`` checks each
    template's existence before adding it to the plan, so the bare
    scaffold (tasks/, logs/, CLAUDE.md) still drops in cleanly.
    """
    return Path(__file__).resolve().parent / "scaffold"


def cmd_init(args: argparse.Namespace) -> None:
    raw_target = args.target_dir
    target = Path.cwd() if raw_target == "." else Path(raw_target).expanduser().resolve()
    source = _find_template_source()
    dry_run = bool(getattr(args, "dry_run", False))

    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}Scaffolding roadrunner project at: {target}")

    if raw_target != "." and not target.exists() and not dry_run:
        target.mkdir(parents=True)

    plan = _init_plan(target, source)
    created: list[str] = []
    skipped: list[str] = []
    for kind, dest, src, content in plan:
        rel = dest.relative_to(target) if dest.is_relative_to(target) else dest
        if kind == "mkdir":
            if dest.exists():
                if not dest.is_dir():
                    skipped.append(f"{rel} (exists, not a directory)")
                continue
            if dry_run:
                print(f"{prefix}mkdir  {rel}/")
            else:
                dest.mkdir(parents=True, exist_ok=True)
                print(f"mkdir  {rel}/")
            created.append(f"{rel}/")
        elif kind == "write":
            if dest.exists():
                print(f"skip   {rel} (already exists)")
                skipped.append(str(rel))
                continue
            if dry_run:
                print(f"{prefix}write  {rel}")
            else:
                dest.write_text(content or "", encoding="utf-8")
                print(f"write  {rel}")
            created.append(str(rel))
        elif kind == "copy":
            assert src is not None, "copy plan entries always carry a source path"
            if dest.exists():
                print(f"skip   {rel} (already exists)")
                skipped.append(str(rel))
                continue
            if dry_run:
                print(f"{prefix}copy   {rel}  <-  {src}")
            else:
                shutil.copy2(src, dest)
                if dest.suffix == ".sh":
                    dest.chmod(dest.stat().st_mode | 0o111)
                print(f"copy   {rel}  <-  {src}")
            created.append(str(rel))

    print()
    print("Next steps:")
    print("  1. Edit tasks/tasks.yaml — replace TASK-001 with your real first task.")
    print("  2. Review CLAUDE.md and tailor the agent brief to your project.")
    print("  3. Confirm .claude/settings.json wires up the hooks you want to run.")
    print("  4. Run `roadrunner status` to confirm the roadmap parses.")
    print("  5. Review a run policy, then use `roadrunner doctor` and `roadrunner run --plan tasks/tasks.yaml --policy POLICY.json`.")
    if skipped:
        print()
        print(f"Skipped {len(skipped)} existing path(s); they were left untouched.")


def _load_tasks_from(path: Path) -> list[dict]:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("tasks") or [])


def _find_cycles(ids: list[str], dep_map: dict[str, list[str]]) -> list[list[str]]:
    """DFS-based cycle detection. Returns each unique cycle as a list of task IDs."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in ids}
    cycles: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def walk(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        for dep in dep_map.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GRAY and dep in stack:
                cycle = stack[stack.index(dep) :] + [dep]
                key = tuple(sorted(set(cycle)))
                if key not in seen:
                    seen.add(key)
                    cycles.append(cycle)
            elif color[dep] == WHITE:
                walk(dep, stack + [dep])
        color[node] = BLACK

    for tid in ids:
        if color[tid] == WHITE:
            walk(tid, [tid])
    return cycles


def _longest_chain(ids: list[str], dep_map: dict[str, list[str]]) -> int:
    """Length of the longest dependency chain in task count. Caller must ensure the graph is acyclic."""
    memo: dict[str, int] = {}

    def depth(tid: str) -> int:
        if tid in memo:
            return memo[tid]
        deps = [d for d in dep_map.get(tid, []) if d in dep_map]
        memo[tid] = 1 + max((depth(d) for d in deps), default=0)
        return memo[tid]

    return max((depth(t) for t in ids), default=0)


def cmd_analyze(args: argparse.Namespace) -> None:
    path = Path(args.tasks_file).expanduser().resolve() if args.tasks_file else TASKS_FILE
    try:
        tasks = _load_tasks_from(path)
    except FileNotFoundError:
        print(f"error: tasks file not found at {path}", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as exc:
        print(f"error: invalid YAML in {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    total = len(tasks)
    counts = {"done": 0, "todo": 0, "in_progress": 0, "blocked": 0, "other": 0}
    for t in tasks:
        status = t.get("status", "todo")
        counts[status if status in counts else "other"] += 1

    ids = [t["id"] for t in tasks if t.get("id")]
    id_set = set(ids)
    dep_map: dict[str, list[str]] = {t["id"]: list(t.get("depends_on") or []) for t in tasks if t.get("id")}

    errors: list[str] = []
    if len(ids) != len(id_set):
        errors.append("Duplicate task IDs")
    warnings: list[str] = []

    for tid, deps in dep_map.items():
        for dep in deps:
            if dep not in id_set:
                errors.append(f"task {tid} depends on unknown task {dep!r}")

    cycles = _find_cycles(ids, dep_map)
    for cycle in cycles:
        errors.append(f"circular dependency: {' -> '.join(cycle)}")

    for t in tasks:
        if not (t.get("validation_commands") or []):
            warnings.append(f"task {t.get('id', '<no-id>')} has no validation_commands")

    critical_path = _longest_chain(ids, dep_map) if not cycles else 0

    print(f"Analyzed: {path}")
    print(f"Total tasks: {total}")
    print(f"  done:        {counts['done']}")
    print(f"  todo:        {counts['todo']}")
    print(f"  in_progress: {counts['in_progress']}")
    print(f"  blocked:     {counts['blocked']}")
    if counts["other"]:
        print(f"  other:       {counts['other']}")
    if critical_path:
        print(f"Critical path (longest dep chain): {critical_path} tasks")

    # ROAD-012: tasks grouped by model hint. "(unset)" bucket counts tasks that
    # rely on the operator's global default. Only printed when at least one
    # task carries the field so the analyze output stays quiet for projects
    # that don't use per-task models.
    model_counts: dict[str, int] = {}
    for t in tasks:
        bucket = t.get("model") or "(unset)"
        model_counts[bucket] = model_counts.get(bucket, 0) + 1
    if any(k != "(unset)" for k in model_counts):
        print()
        print("Tasks by model:")
        # Sort: unset bucket last, others alphabetically by model ID.
        ordered = sorted(model_counts.items(), key=lambda kv: (kv[0] == "(unset)", kv[0]))
        for model, count in ordered:
            print(f"  {model:<24} {count}")

    if errors:
        print()
        print(f"Errors ({len(errors)}):")
        for e in errors:
            print(f"  ❌ {e}")
    if warnings:
        print()
        print(f"Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"  ⚠️  {w}")

    if not errors and not warnings:
        print()
        print("✅ No issues found.")

    sys.exit(1 if errors else 0)


def cmd_session_start(args: argparse.Namespace) -> None:
    """Called by the SessionStart hook. Emits a `hookSpecificOutput` JSON whose
    `additionalContext` reads as an INSTRUCTION, not ambient status — so the
    agent starts working on turn 1 without the user needing to type "Begin."

    Decision tree:
      1. Active in-progress task in tasks.yaml → emit the resume brief
      2. No active, but an eligible next task exists → emit a "your next action
         is `roadrunner start ...`" directive followed by the brief
      3. Blocked tasks but none eligible → report the blocked list
      4. All tasks done → prompt for ROADMAP_COMPLETE sentinel
      5. Tasks.yaml unreadable or empty → silent no-op (don't poison context)

    Reads live state from tasks.yaml rather than the snapshot file so a stale
    or missing snapshot can't produce a misleading directive.
    """
    try:
        tasks = load_tasks()
    except (FileNotFoundError, ValueError):
        # No roadmap present (or malformed) — stay silent.
        return
    if not tasks:
        return

    # Drop a session shell so finalize_current() has a boundary to replay
    # against. open_session() also finalizes any prior open session, so a
    # crashed previous run gets summarized here rather than left orphaned.
    try:
        rr_session.open_session()
    except OSError as exc:
        print(f"[roadrunner] session open failed: {exc}", file=sys.stderr)

    # ROAD-014: Claude Code's SessionStart hook payload includes `session_id`.
    # Capture it into .roadmap_state.json so `roadrunner resume --session-id`
    # can hand the operator a `claude --resume <id>` command after a crash.
    # Tolerant of an empty/missing/malformed payload (operator may invoke
    # session-start by hand) — a missing session_id leaves last_session_id
    # untouched rather than clearing the prior value.
    captured_session_id: str | None = None
    try:
        if not sys.stdin.isatty():
            raw_payload = sys.stdin.read()
            if raw_payload.strip():
                payload = json.loads(raw_payload)
                if isinstance(payload, dict):
                    sid = payload.get("session_id")
                    if isinstance(sid, str) and sid.strip():
                        captured_session_id = sid.strip()
    except (OSError, ValueError, json.JSONDecodeError):
        # Hook plumbing is best-effort observability; never block the loop on it.
        captured_session_id = None

    # ROAD-010: SessionStart is the boundary that resets the per-session iteration
    # counter. Persist the reset atomically so the first Stop-hook fire of the
    # new session sees session_iteration=0 → 1 rather than carrying over the
    # previous run's count. The read MUST happen inside the lock — otherwise a
    # concurrent check-stop fire between read and write can have its iteration
    # / attempts_per_task updates rolled back by this snapshot.
    with _exclusive_state_lock():
        state = read_state()
        iteration = state.get("iteration", 0)  # lifetime (display only, not incremented here)
        write_state(
            state.get("current_task_id"),
            iteration,
            state.get("attempts_per_task"),
            session_iteration=0,
            # ROAD-011: parallel to session_iteration — cost is per-session and
            # resets on the SessionStart boundary so the new run gets a fresh
            # budget allowance.
            session_cost_usd=0.0,
            # ROAD-014: persist the freshly-captured Claude Code session ID
            # (or preserve the prior value when capture failed — None means
            # "no change" per write_state's contract).
            last_session_id=captured_session_id,
        )
    trace_event(
        "session_start_reset",
        task_id=state.get("current_task_id"),
        iteration=iteration,
        extra={
            "session_iteration": 0,
            "session_cost_usd": 0.0,
            "session_id_captured": captured_session_id is not None,
        },
    )

    # After reset, display starts from session_iteration=0; the first cap-check
    # will see 1. Default session cap is 100 (ROAD-010).
    session_display = 0

    in_flight = active_task(tasks)
    if in_flight is not None:
        brief = _build_task_brief(in_flight, session_display, 100, resume=True)
        message = "You are resuming a roadrunner-driven session. A task is in progress — continue from where it left off.\n\n" + brief
    else:
        next_task = next_eligible_task(tasks)
        if next_task is not None:
            brief = _build_task_brief(next_task, session_display, 100, resume=False)
            message = (
                "You are resuming a roadrunner-driven session. No task is "
                "in progress. Your first action:\n\n"
                f"    roadrunner start {next_task['id']}\n\n"
                "Then follow the brief below.\n\n" + brief
            )
        else:
            blocked = [t.get("id") for t in tasks if t.get("status") == "blocked"]
            if blocked:
                message = (
                    "Roadrunner session resumed, but no tasks are eligible. "
                    f"Blocked tasks: {blocked}. Investigate each blocker, or "
                    "output `ROADMAP_COMPLETE` on its own line to halt."
                )
            else:
                remaining = [t.get("id") for t in tasks if t.get("status") != "done"]
                if remaining:
                    message = (
                        "Roadrunner session resumed. No tasks are eligible "
                        f"but these aren't done: {remaining}. Check dependencies "
                        "and status in tasks/tasks.yaml."
                    )
                else:
                    message = (
                        "Roadrunner session resumed. All tasks are done. "
                        "Output `ROADMAP_COMPLETE` on its own line as the last "
                        "line of your next message to halt the loop cleanly."
                    )

    # ROAD-013: prepend the last N learnings entries so operational facts
    # discovered in earlier sessions survive the context boundary. Skipped
    # when the file has no entries so a fresh project doesn't get a
    # "Learnings (0):" header floating in its bootstrap context.
    tail = _learnings_tail()
    if tail:
        learnings_block = "Operator learnings (most recent first):\n" + "\n".join(f"  - {line}" for line in reversed(tail))
        message = learnings_block + "\n\n" + message

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": message,
                }
            },
            ensure_ascii=False,
        )
    )


def _build_task_brief(task: Task, iteration: int, max_iter: int, resume: bool = False) -> str:
    criteria = "\n".join(f"  - {ac}" for ac in task.get("acceptance_criteria", []))
    validation = "\n".join(f"  - {v}" for v in task.get("validation_commands", []))
    files = "\n".join(f"  - {f}" for f in task.get("files_expected", []))
    header = (
        f"RESUME IN-PROGRESS TASK. Iteration {iteration}/{max_iter}." if resume else f"Continue working. Iteration {iteration}/{max_iter}."
    )
    # Avoid embedding the completion sentinel as a bare line — describe it instead.
    sentinel_hint = f"output the completion sentinel (the word {_COMPLETION_SIGNAL}) on its own line"
    # ROAD-012: surface the per-task model hint so the agent knows it was
    # down/upshifted from the operator's default. Empty when the task omits
    # the field — most tasks will, so the brief stays clean.
    model = task.get("model")
    model_line = f"Model: {model}\n" if model else ""
    # Issue #21: retries are not blind. Surface the last validation failure for this task.
    last = read_state().get("last_failure") or {}
    prior = last.get("summary") if last.get("task_id") == task["id"] else ""
    prior_block = f"Previous validation failed — {prior}\nFix this cause; do not repeat the same change.\n\n" if prior else ""
    return (
        f"{header}\n\n"
        f"CURRENT TASK: {task['id']} — {task.get('title')}\n"
        f"{model_line}"
        f"Goal: {task.get('goal', 'N/A')}\n\n"
        f"{prior_block}"
        f"Acceptance criteria:\n{criteria or '  (none specified)'}\n\n"
        f"Validation commands (must pass before complete):\n{validation or '  (none)'}\n\n"
        f"Expected files:\n{files or '  (none)'}\n\n"
        f"When done: run `roadrunner complete {task['id']} --notes '...'`\n"
        f"To signal full roadmap done: {sentinel_hint}."
    )


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        from .doctor import main as doctor_main
        sys.exit(doctor_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "deliver":
        from .delivery import main as delivery_main
        sys.exit(delivery_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        from .intake import main as sync_main
        sys.exit(sync_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        from .supervisor import main as run_main
        sys.exit(run_main(sys.argv[2:]))
    parser = argparse.ArgumentParser(description="Roadmap Loop Controller")
    sub = parser.add_subparsers(dest="command")
    for name, description in (("run", "Execute a frozen plan with verified local integration"),
                              ("sync", "Normalize and freeze selected roadmap or GitHub items"),
                              ("deliver", "Prepare or explicitly execute policy-controlled remote delivery"),
                              ("doctor", "Preflight project, policy, adapter, and isolation")):
        sub.add_parser(name, help=description)

    sub.add_parser("status")
    sub.add_parser("next")
    sub.add_parser("health")
    sub.add_parser("pause")
    p_resume = sub.add_parser(
        "resume",
        help=(
            "Without flags: clear the .roadrunner_paused marker. "
            "With --session-id: print the `claude --resume <id>` command for "
            "the captured Claude Code session. With --exec: run it directly."
        ),
    )
    # ROAD-014: --session-id is a flag (no value) that switches resume into
    # "print the captured claude --resume command" mode. --exec switches into
    # the same lookup but exec's in place. dest=exec_ avoids the Python keyword.
    resume_group = p_resume.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--session-id",
        dest="session_id",
        action="store_true",
        help="Print `claude --resume <id>` for the most recent captured session.",
    )
    resume_group.add_argument(
        "--exec",
        dest="exec_",
        action="store_true",
        help="Like --session-id, but exec the command directly instead of printing it.",
    )
    sub.add_parser("snapshot")
    sub.add_parser("session-start")
    sub.add_parser(
        "post-compact",
        help="PostCompact hook entry: verify .context_snapshot.json after compaction",
    )

    p_sessions = sub.add_parser(
        "sessions",
        help="Pretty-print recent session summaries (newest first).",
    )
    p_sessions.add_argument(
        "--last",
        default=None,
        help="How many recent sessions to show (default: 10).",
    )

    p_start = sub.add_parser("start")
    p_start.add_argument("task_id")

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("task_id")

    p_complete = sub.add_parser("complete")
    p_complete.add_argument("task_id")
    p_complete.add_argument("--notes", default="")

    p_block = sub.add_parser("block")
    p_block.add_argument("task_id")
    p_block.add_argument("--notes", default="")

    p_reset = sub.add_parser("reset")
    p_reset.add_argument("task_id")
    p_reset.add_argument("--summary", default="")

    p_reset_iter = sub.add_parser(
        "reset-iteration",
        help="ROAD-010: reset the session (and optionally lifetime) iteration counter.",
    )
    p_reset_iter_group = p_reset_iter.add_mutually_exclusive_group()
    p_reset_iter_group.add_argument(
        "--soft",
        action="store_true",
        help="Reset session_iteration only; preserve lifetime counter (default).",
    )
    p_reset_iter_group.add_argument(
        "--hard",
        action="store_true",
        help="Reset both session_iteration and lifetime iteration. Destructive.",
    )

    p_stop = sub.add_parser("check-stop")
    # ROAD-010: session-iteration cap default raised from 50 → 100.
    p_stop.add_argument("--max-iterations", default="100")
    p_stop.add_argument("--max-attempts", default=str(MAX_TASK_ATTEMPTS))
    # ROAD-011: per-session USD budget cap. Falls back to ROADMAP_MAX_BUDGET_USD
    # env var when the flag is absent; feature disabled when both are unset.
    p_stop.add_argument(
        "--max-budget-usd",
        default=None,
        help=(
            "Per-session USD budget cap. Halts the loop with a hard stop when "
            "the running session cost meets or exceeds this value. Overrides "
            "ROADMAP_MAX_BUDGET_USD. Feature is off by default."
        ),
    )

    p_init = sub.add_parser("init", help="Scaffold a new roadrunner project directory")
    p_init.add_argument("target_dir", help="Target directory (use '.' for current working directory)")
    p_init.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without touching the filesystem",
    )

    p_analyze = sub.add_parser("analyze", help="Analyze tasks.yaml for cycles, missing deps, and coverage")
    p_analyze.add_argument(
        "--tasks-file",
        default=None,
        help="Path to a tasks.yaml file (defaults to the project tasks/tasks.yaml)",
    )

    p_watch = sub.add_parser(
        "watch",
        help="Live read-only monitor: poll disk state and redraw a status frame.",
    )
    p_watch.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between frames (floored at 0.5). Default: 5.",
    )

    p_commit = sub.add_parser(
        "commit",
        help="Scope-aware commit: stages only files in the task's files_expected + roadrunner overlay",
    )
    p_commit.add_argument("task_id")
    p_commit.add_argument("--notes", default="", help="Commit body (subject auto-generated from task)")
    p_commit.add_argument(
        "--type",
        default=None,
        help="Conventional commit type (feat/fix/chore/docs/refactor/test/...). Defaults to 'feat'.",
    )

    args = parser.parse_args()

    dispatch = {
        "status": cmd_status,
        "next": cmd_next,
        "start": cmd_start,
        "validate": cmd_validate,
        "complete": cmd_complete,
        "block": cmd_block,
        "reset": cmd_reset,
        "reset-iteration": cmd_reset_iteration,
        "health": cmd_health,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "check-stop": cmd_check_stop,
        "snapshot": cmd_snapshot,
        "session-start": cmd_session_start,
        "post-compact": cmd_post_compact,
        "init": cmd_init,
        "analyze": cmd_analyze,
        "commit": cmd_commit,
        "watch": cmd_watch,
        "sessions": cmd_sessions,
    }

    if args.command not in dispatch:
        parser.print_help()
        sys.exit(1)

    dispatch[args.command](args)


if __name__ == "__main__":
    main()
