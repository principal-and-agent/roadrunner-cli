# Claude Code project hooks

The interactive hook workflow registers these scripts in `.claude/settings.json`. `roadrunner init` ships the same scripts and settings for new projects.

| Event | Script | Behavior |
| --- | --- | --- |
| PreToolUse | `protect_roadmap_hook.sh` | Blocks Claude Write and Edit operations on the project roadmap. |
| PostToolUse | `post_write_hook.sh` | Checks Python with Ruff when available and parses YAML. Reports feedback without blocking. |
| SessionStart | `session_start_hook.sh` | Reads the live roadmap, injects a brief and operator learnings, and resets per-session counters. |
| Stop | `stop_hook.sh` | Respects pause, applies session limits, and supplies the next action while work remains. |
| PreCompact | `precompact_hook.sh` | Writes the context snapshot before compaction. |
| PostCompact | `postcompact_hook.sh` | Checks the context snapshot after compaction. |

A PreToolUse exit code of 2 denies the tool call. Stop communicates its decision as JSON. See the [hook contracts](https://github.com/principal-and-agent/roadrunner-cli/blob/main/docs/architecture.md#2-hook-contracts).
