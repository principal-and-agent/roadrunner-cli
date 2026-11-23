#!/usr/bin/env bash
# PreToolUse (Write|Edit) — block direct edits to this project's roadmap.
set -euo pipefail

exec python3 -c '
import json
import sys
from pathlib import Path

project = Path(sys.argv[1]).parent.parent.resolve()
try:
    payload = json.load(sys.stdin)
    file_path = payload["tool_input"]["file_path"]
except (ValueError, KeyError, TypeError):
    sys.exit(0)
if not isinstance(file_path, str) or not file_path:
    sys.exit(0)

try:
    cwd = Path(payload.get("cwd") or project)
    target = (cwd / file_path).resolve()
    roadmap = (project / "tasks" / "tasks.yaml").resolve()
    # samefile also handles alternate spellings on case-insensitive volumes.
    protected = target == roadmap or target.samefile(roadmap)
except (OSError, RuntimeError, ValueError):
    sys.exit(0)

if protected:
    print("Direct edits to tasks/tasks.yaml are blocked. Use roadrunner start, roadrunner complete, "
          "or roadrunner block to change task status. Ask the operator for roadmap definition changes.", file=sys.stderr)
    sys.exit(2)
' "$0"
