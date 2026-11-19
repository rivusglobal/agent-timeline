---
name: agent-timeline
description: One-click zoomable timeline of where an AI coding agent session's time went by phase (implementing, local commands/tests, waiting/polling, git/PR, delegation, async wait, ...). Works for Claude Code, Codex CLI and OpenCode. Use when the user asks for a session timeline, time breakdown, "where did the time go", or runs /agent-timeline. Works in any repo.
---

# Session timeline

Generates a self-contained interactive HTML (wheel zoom, drag pan, minimap, phase filters, per-window stats) from an agent's own session log. The skill renders the page itself (`template.html`); no design tool or Artifact is needed. Hover a block to see what was happening; click to pin it. Each block carries a short label from the tool call itself (a Bash description or first command line, a file name, a search pattern, a subagent task), redacted for obvious secrets and truncated to 140 characters. Human-wait blocks also carry a 200-character redacted snippet of the agent's last message and of the prompt that ended the wait; no other message text is stored. The page can still show command lines and file names, so pass `--no-detail` before sharing it; that leaves phase names and durations only.

## Run

```bash
python3.12 ~/.claude/skills/agent-timeline/agent_timeline.py --open
```

- No argument: newest session across all supported agents (Claude prefers the current directory's project).
- `--agent claude|codex|opencode` limits discovery; `--session <id>` picks one (id prefix; full id for opencode).
- Or pass a path: a `.jsonl` (format detected) or `opencode.db`. `--out FILE` sets the output; default `$TMPDIR/agent-timeline-<id>.html`.

## After running

Say the output path and give 2-3 findings from the numbers (largest active phase, async wait share, polling). Publish as a private Artifact only if the user asks to share it.

## Supported agents

| Agent | Source | Timing quality |
|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl` | per tool call (use and result timestamps) |
| Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl` | per tool call; verified on hand-built fixtures, no real rollout was available |
| OpenCode | `~/.local/share/opencode/opencode.db` | per assistant message: model time = created to streamed, tool time = streamed to completed (no per-tool times) |
| Cursor | `~/.cursor/projects/*/agent-transcripts` | not supported: transcripts carry no timestamps |
| Aider, others | | not supported yet |

## Add an agent

Write `read_<agent>(path) -> phase_report._Transcript` in `adapters.py`: assistant turns (`_Message` with first/last timestamp and `_ToolUse` list), `result_ts[tool_call_id]`, and `_UserEvent`s. Route tool names through `adapters.normalize` so the shell-command classifier applies. Add the agent to `find_transcript`, `detect` and `read` in `agent_timeline.py`, and add a fixture test in the rivus repo with known phase seconds.

## Notes

- Tests and the synthetic sample session live at the repo root (`tests/`, `scripts/make_sample.py`).
- Context compaction (Claude Code `compact_boundary` record, which carries its duration and token counts) is its own phase. Its time is carved out of the surrounding wait or model time, and the synthetic `isCompactSummary` message is not counted as a human prompt. Other agents' compaction is not detected.
- The page has a "How these numbers are computed" section; keep it in step with the method.
- Intervals over 30 min are `idle (long gap)`, drawn 90 s wide so they do not swallow the axis; the axis and tooltips show real time.
