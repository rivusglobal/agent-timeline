Show where this machine's most recent agent session spent its time.

1. Run `python3.12 skills/agent-timeline/agent_timeline.py --open` from the repo root. If this repo is not the workspace, use the path of the cloned `agent-timeline/skills/agent-timeline/agent_timeline.py` instead.
2. Add `--agent claude`, `--agent codex` or `--agent opencode` to pick a tool, `--session <id-prefix>` to pick a session, or pass a transcript path.
3. Say the output path, then give two or three findings from the numbers: the largest active phase, how much of the wall clock was waiting on the human, and any polling.

Cursor's own agent transcripts carry no per-call timestamps, so they cannot be drawn. This command reads Claude Code, Codex CLI and OpenCode sessions.

Before sharing the page, rerun with `--no-detail`: it keeps phase names and durations only.
