"""Readers that turn Codex CLI and OpenCode session logs into the analyzer's transcript model.

Each reader returns ``session_phase_report.Transcript`` (assistant turns with first/last timestamps and tool calls,
tool-result timestamps by call id, user events). Tool names are normalized to Claude Code's so one classifier serves
every agent. Only names and timestamps are kept; no prompts or outputs are retained beyond classification.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import phase_report as pr

_SHELL = {
    "bash",
    "shell",
    "exec_command",
    "run_terminal_cmd",
    "run_shell_command",
    "local_shell",
    "execute_bash",
    "terminal",
}
_EDIT = {
    "apply_patch",
    "edit",
    "write",
    "patch",
    "str_replace",
    "str_replace_editor",
    "multiedit",
    "replace",
    "write_file",
    "create_file",
}
_READ = {
    "read",
    "read_file",
    "grep",
    "glob",
    "list",
    "ls",
    "search",
    "codebase_search",
    "file_search",
    "webfetch",
    "websearch",
    "web_search",
    "list_dir",
    "view",
}
_DELEGATE = {"task", "agent", "subagent", "spawn_agent", "delegate"}
_CODEX_CALLS = ("function_call", "custom_tool_call", "local_shell_call")
_CODEX_OUTPUTS = ("function_call_output", "custom_tool_call_output")
_CODEX_ASSISTANT_ITEMS = ("reasoning", "message", *_CODEX_CALLS)
_MS = 1000.0


def normalize(name: str, command: str) -> tuple[str, dict[str, str]]:
    """Map an agent's tool name to Claude Code's (name, input) so ``classify_tool`` applies."""
    low = name.lower()
    if low in _SHELL:
        return "Bash", {"command": command}
    if low in _EDIT:
        return "Edit", {}
    if low in _READ:
        return "Read", {}
    if low in _DELEGATE:
        return "Agent", {}
    return name, {}


def _iso(raw: str) -> float:
    return pr.epoch_seconds(raw)


def _tool(call_id: str, name: str, command: str) -> pr.ToolUse:
    norm, tool_input = normalize(name, command)
    label = (
        pr.label_tool("Bash", tool_input) if norm == "Bash" else pr.redact(name)[: pr.LABEL_LIMIT]
    )
    return pr.ToolUse(call_id, pr.classify_tool(norm, tool_input), False, label)


def _command_of(args: object) -> str:
    """The command text out of a decoded function_call `arguments` or an action dict."""
    if not isinstance(args, dict):
        return ""
    cmd = args.get("command") or args.get("cmd") or ""
    return " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)


def _codex_record(line: str) -> tuple[dict[str, object], float] | None:
    """(record, timestamp) for one rollout line, or None when the line is not a timed JSON object."""
    try:
        rec = json.loads(line)
        return rec, _iso(rec["timestamp"])
    except (json.JSONDecodeError, RecursionError, KeyError, ValueError, TypeError, AttributeError):
        return None


def _call_arguments(pay: dict[str, object]) -> object | None:
    """Decoded arguments of a call, or None when `arguments` is not valid JSON. Raw `input` text (a patch) is not JSON."""
    raw = pay.get("arguments")
    if not isinstance(raw, str):
        return raw or pay.get("action")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _add_codex_call(out: pr.Transcript, cur: pr.Message, pay: dict[str, object]) -> None:
    cid = pay.get("call_id") or pay.get("id")
    args = _call_arguments(pay)
    if cid is None or (args is None and "arguments" in pay):
        out.skipped_lines += (
            1  # a call with no id, or arguments that are not JSON, cannot be timed or classified
        )
        return
    cur.tools.append(_tool(str(cid), str(pay.get("name") or "shell"), _command_of(args)))


def read_codex(path: Path) -> pr.Transcript:
    """Codex CLI rollout: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, lines {timestamp, type, payload}."""
    out = pr.Transcript()
    cur: pr.Message | None = None
    turns = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            parsed = _codex_record(line)
            if parsed is None:
                out.skipped_lines += 1
                continue
            rec, ts = parsed
            raw_pay = rec.get("payload")
            pay: dict[str, object] = raw_pay if isinstance(raw_pay, dict) else {}
            kind = pay.get("type")
            if rec.get("type") == "event_msg" and kind == "user_message":
                out.user_events.append(pr.UserEvent(ts, human=True))
                cur = None
            elif rec.get("type") != "response_item":
                continue
            elif kind in _CODEX_OUTPUTS:
                if pay.get("call_id") is None:
                    out.skipped_lines += 1
                else:
                    out.result_ts[str(pay["call_id"])] = ts
                out.user_events.append(pr.UserEvent(ts, human=False))
                cur = None
            elif kind in _CODEX_ASSISTANT_ITEMS and not (
                kind == "message" and pay.get("role") != "assistant"
            ):
                if cur is None:
                    turns += 1
                    cur = out.messages.setdefault(f"t{turns}", pr.Message(ts, ts))
                cur.last_ts = max(cur.last_ts, ts)
                if kind in _CODEX_CALLS:
                    _add_codex_call(out, cur, pay)
    return out


OPENCODE_TABLES = ("session_v2", "session_message")


def _ms(time: dict[str, object], key: str, mid: str) -> float:
    value = time.get(key)
    if not isinstance(value, (int, float)):
        raise TypeError(f"opencode message {mid}: data.time.{key} is missing or not a number")
    return value / _MS


def _opencode_message(mid: str, data: str, out: pr.Transcript) -> None:
    """Add one assistant row: model time = created to streamed, tool time = streamed to completed."""
    d = json.loads(data)
    time = d.get("time") if isinstance(d, dict) else None
    if not isinstance(time, dict):
        raise TypeError(f"opencode message {mid}: data.time is missing")
    start, streamed, done = (_ms(time, k, mid) for k in ("created", "streamed", "completed"))
    msg = pr.Message(start, streamed)
    for i, block in enumerate(d.get("content", [])):
        if isinstance(block, dict) and block.get("type") == "tool":
            cid = str(block.get("id") or f"{mid}.{i}")
            state = block.get("state")
            command = _command_of(state.get("input")) if isinstance(state, dict) else ""
            msg.tools.append(_tool(cid, str(block.get("name")), command))
            out.result_ts[cid] = (
                done  # no per-tool times: the tool phase runs until the message completes
            )
    out.messages[mid] = msg
    if (
        msg.tools
    ):  # a tool-less reply is followed by a wait, not by a tool result that resumes the agent
        out.user_events.append(pr.UserEvent(done, human=False))


def read_opencode(db: Path, session: str | None = None) -> pr.Transcript:
    """OpenCode sqlite: session_message rows; assistant data has time.{created,streamed,completed} in ms."""
    out = pr.Transcript()
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        missing = [t for t in OPENCODE_TABLES if t not in tables]
        if missing:
            raise ValueError(f"{db}: not an opencode database, missing table(s) {missing}")
        sid = session
        if sid is None:
            row = con.execute(
                "select id from session_v2 order by time_updated desc limit 1"
            ).fetchone()
            if row is None:
                raise ValueError(f"no opencode sessions in {db}")
            sid = row[0]
        rows = con.execute(
            "select id, type, time_created, data from session_message where session_id=? order by seq",
            (sid,),
        )
        for mid, mtype, created, data in rows:
            if mtype == "user":
                out.user_events.append(pr.UserEvent(created / _MS, human=True))
            elif mtype == "assistant":
                _opencode_message(mid, data, out)
    finally:
        con.close()
    return out
