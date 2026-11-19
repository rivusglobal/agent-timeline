#!/usr/bin/env python3.12
"""Where did a Claude Code session's wall-clock time go, by development phase.

Reads transcripts (``~/.claude/projects/<proj>/<session>.jsonl``, subagent files
too) and splits wall time into phases a developer cares about:

* ``implementing``    model time spent producing Edit/Write calls + their execution
* ``local commands``  tests, lint, type-check, builds (``make test*``, pytest, ruff, mypy ...)
* ``waiting/polling`` sleep/until loops, Monitor, ScheduleWakeup, TaskOutput
* ``git/PR``          git and gh commands
* ``reading``         Read/Grep/Glob/Web/ToolSearch
* ``delegation``      Agent/Workflow/SendMessage calls
* ``other shell`` / ``other tools``
* ``thinking/answering`` model time on turns that call no tool
* ``async wait``      the session idle while a background job/subagent runs (attributed to the
                      phase of the last background launch, e.g. ``local commands (background)``)
* ``human wait``      idle until the human types the next prompt
* ``idle (long gap)`` ANY interval over 30 minutes (overnight, laptop asleep, a permission prompt
                      nobody answered): reported apart and excluded from "% active"

Method (transcripts store no durations, so they are derived from timestamps): for each assistant
message, *model time* = last line of the message minus the previous user/tool_result event; *tool
time* = last tool_result minus the message end, charged to the phase of the slowest tool in that
message (parallel calls share one wall interval); a turn with no tool call is followed by an idle
gap charged to human wait (the next event is a human prompt) or async wait (a hook/notification).

Reading the numbers:
* Totals are SESSION-SECONDS summed over files. Concurrent sessions add up, and a synchronous
  subagent call is charged to the parent as ``delegation`` and again in the subagent's own file,
  so main sessions and subagents are reported in separate sections and must not be added.
* A background command's own runtime is only visible as the async-wait gap that follows it.
* Timestamps are written when a content block completes, so model time is approximate.
* Directory mode filters on file mtime, so a session touched recently counts its whole history.
* Lines that are not valid transcript records are skipped and counted, as are tool calls that
  never got a result (interrupted).
* Bash classification is a heuristic on the command text: a wait word counts only at command
  position (any ``while`` loop counts as waiting, a newline starts a new command, so a heredoc
  body line that begins with ``sleep`` is a known false positive; so are prose like ``then watch``,
  brace text like ``{while: 1}``, and the misses ``timeout -k 5 10 sleep``, ``bash -xc`` and
  ``FOO="a b" sleep``, which classify as other shell).

Usage: ``python3.12 scripts/session_phase_report.py <transcript.jsonl | dir> [--days N]``
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

DESCRIPTION = "Wall-clock time per development phase from Claude Code transcripts."
HUMAN_ORIGIN_KIND = "human"
SUBAGENT_DIR = "subagents"
RECORD_TYPES = ("assistant", "user")


class Phase(StrEnum):
    IMPLEMENT = "implementing"
    COMMANDS = "local commands"
    WAITING = "waiting/polling"
    GIT = "git/PR"
    READING = "reading"
    DELEGATION = "delegation"
    SHELL = "other shell"
    TOOLS = "other tools"
    THINKING = "thinking/answering"
    COMPACTION = "context compaction"
    ASYNC_WAIT = "async wait"
    HUMAN_WAIT = "human wait"
    IDLE = "idle (long gap)"


# Any interval longer than this is the session sitting unattended, not working or waiting on a job.
IDLE_CUTOFF_S = 1800.0
NOT_ACTIVE = (Phase.HUMAN_WAIT, Phase.IDLE)
MINUTES_THRESHOLD_S = 90.0

_WAIT_TOOLS = {"ScheduleWakeup", "Monitor", "TaskOutput", "TaskStop"}
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
_READ_TOOLS = {"Read", "Grep", "Glob", "ToolSearch", "WebFetch", "WebSearch", "LSP"}
_DELEGATE_TOOLS = {"Agent", "Workflow", "SendMessage", "Task"}
# Command position = line start, after ; & | ( or a newline, after do/then/else, or after `{`.
_WAIT_RE = re.compile(
    r"(^|[;&|(\n][ \t]*|\b(?:do|then|else)\s+|\{\s*)(sleep|until|while|pgrep|watch)\b|\bjob_liveness\b"
)
# Wrappers stripped before looking at the command itself: `cd dir &&`, `FOO=1`, time/nohup/exec/env/command,
# `timeout 60`, and `bash -c '`.
_PREFIX_RE = re.compile(
    r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*|\w+=\S*\s+|(?:time|nohup|exec|command|env)\s+|timeout\s+\S+\s+|(?:ba|z)?sh\s+-l?c\s+['\"]?)+"
)
_COMMANDS_RE = re.compile(
    r"\bmake\s+(test|lint|rivus-validate|setup)|\bpytest\b|\bvitest\b|\bnpm\s+(run\s+)?(test|lint|build)\b"
    r"|\bruff\b|\bmypy\b|\bblack\b|\btsc\b|selftest|\bbounded_test_gate\b"
)
_LEADING_GIT_RE = re.compile(r"^\s*(git|gh)\s")
_GIT_RE = re.compile(r"(^|[;&|(\n][ \t]*)(git|gh)\s")


def classify_tool(name: str, tool_input: dict[str, Any]) -> Phase:
    """Phase of one tool call. A command that STARTS with git/gh is git even if its text says "sleep"."""
    if name in _WAIT_TOOLS:
        return Phase.WAITING
    if name in _EDIT_TOOLS:
        return Phase.IMPLEMENT
    if name in _READ_TOOLS:
        return Phase.READING
    if name in _DELEGATE_TOOLS:
        return Phase.DELEGATION
    if name == "Bash":
        command = _PREFIX_RE.sub("", str(tool_input.get("command", "")))
        if _LEADING_GIT_RE.search(command):
            return Phase.GIT
        if _WAIT_RE.search(command):
            return Phase.WAITING
        if _COMMANDS_RE.search(command):
            return Phase.COMMANDS
        if _GIT_RE.search(command):
            return Phase.GIT
        return Phase.SHELL
    return Phase.TOOLS


def epoch_seconds(raw: str) -> float:
    """Epoch seconds of an ISO timestamp; a naive one (no zone) is a ValueError, not a local-time guess."""
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed.timestamp()


@dataclass(frozen=True)
class ToolUse:
    tool_use_id: str
    phase: Phase
    background: bool
    label: str = ""


# Bare user:pw@ text is deliberately over-redacted (echo a:b@c too): a leaked secret costs more than a hidden label.
# Best effort: it catches common credential shapes, not every secret. Only dropping labels guarantees none leak.
# Every quantifier is bounded and the keyword clause starts at a literal, so no input makes it super-linear.
SECRET_RE = re.compile(
    r"(?i)("
    r"bearer\s+[\w.\-~+/=]+"
    r"|authorization:\s*\S+(?:\s+\S+)?"
    r"|\b(?:sk|pk|rk)[_-](?:live|test)[_-]\w{8,}|sk-[\w\-]{8,}|xox[abprs]-[\w-]+|gh[pousr]_\w{16,}"
    r"|github_pat_\w+|glpat-[\w-]+|AIza[\w-]{20,}|AKIA\w{12,}|eyJ[\w-]+\.[\w-]+\.[\w-]+"
    r"|//[^/\s@]+@"
    r"|\b[\w.\-]{1,64}:[^\s/:\"']{1,200}@"
    r"|(?:set-)?cookie:\s{0,3}[^\r\n\"']+"
    r"|(?:pass|secret|token|api[_-]?key|credential|cookie)\w{0,100}\"?"
    r"\s{0,3}[=:]\s{0,3}(?:\"[^\"]*\"|'[^']*'|\S+)"
    r"|--[\w-]{0,30}(?:password|passwd|token|secret|api-key)[= ]\S+"
    r"|\s--cookie(?:\s+|=)(?:\"[^\"]*\"|'[^']*'|\S+)"
    r"|\s-b(?:\s+|(?=[\"']))(?:\"[^\"]*=[^\"]*\"|'[^']*=[^']*'|[^\s\"']*=\S*)"
    r"|(?:sshpass|redis-cli|twine|docker login|az login|xfreerdp)[^|;&\n]{0,80}?\s-[pa](?:\s+|(?=\S))\S+"
    r"|\bxfreerdp\b[^|;&\n]{0,200}?\s/p:\S+"
    r"|\b(?:mysql\w*|mariadb\w*|psql|pg_dump\w*|7z\w*|unzip|zip)\b[^|;&\n]{0,80}?\s-p\S+|\s-u\s{0,3}\S+:\S+"
    r"|[?&](?:key|sid|session\w{0,10}|auth\w{0,10})=[^&\s]+"
    r"|(?:x-amz-signature|signature|sig)=\S+"
    r"|\baws\s+configure\s+set\s+\S+\s+\S+"
    r")"
)
REDACT_INPUT_LIMIT = 4000
LABEL_LIMIT = 140
CONTEXT_LIMIT = 200


def snippet(content: object) -> str:
    """Redacted one-line start of a message's text (a str, or the text blocks of a content list)."""
    if isinstance(content, list):
        content = " ".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    if not isinstance(content, str):
        return ""
    return redact(" ".join(content.split()))[:CONTEXT_LIMIT]


def redact(text: str) -> str:
    """``text`` with credential-looking spans replaced (best effort), after cutting it to a bounded length."""
    return SECRET_RE.sub("[redacted]", text[:REDACT_INPUT_LIMIT])


def label_tool(name: str, tool_input: dict[str, Any], limit: int = LABEL_LIMIT) -> str:
    """Short human label for one tool call (redacted, truncated). Used by the session timeline."""
    if name == "Bash":
        lines = (
            str(tool_input.get("description") or tool_input.get("command") or "")
            .strip()
            .splitlines()
        )
        text = lines[0] if lines else ""
    elif name in _EDIT_TOOLS | {"Read"}:
        text = Path(str(tool_input.get("file_path") or tool_input.get("path") or "")).name
    elif name in {"Grep", "Glob"}:
        text = str(tool_input.get("pattern") or "")
    elif name in _DELEGATE_TOOLS:
        text = str(tool_input.get("description") or tool_input.get("subagent_type") or "")
    else:
        text = str(
            tool_input.get("description") or tool_input.get("query") or tool_input.get("url") or ""
        )
    text = redact(text)
    shown = redact(name)[:limit]
    return f"{shown}: {text[:limit]}" if text else shown


@dataclass
class Message:
    first_ts: float
    last_ts: float
    tools: list[ToolUse] = field(default_factory=list)
    text: str = ""  # last assistant text, redacted and cut (wait-block context only)


@dataclass(frozen=True)
class UserEvent:
    ts: float
    human: bool
    text: str = ""  # the human's prompt, redacted and cut (wait-block context only)


@dataclass
class Transcript:
    messages: dict[str, Message] = field(default_factory=dict)  # insertion order = file order
    user_events: list[UserEvent] = field(default_factory=list)
    result_ts: dict[str, float] = field(default_factory=dict)
    compactions: list[tuple[float, float]] = field(
        default_factory=list
    )  # (start, end) of each context compaction
    compaction_labels: list[str] = field(default_factory=list)
    skipped_lines: int = 0


def merge_compactions(
    compactions: list[tuple[float, float]], labels: list[str]
) -> tuple[list[tuple[float, float]], list[str]]:
    """Sorted, non-overlapping compactions: duplicated or overlapping boundary records (a replayed session) merge.

    A merged span keeps the label of its longest member.
    """
    merged: list[tuple[float, float]] = []
    merged_labels: list[str] = []
    longest: list[float] = []
    for (cs, ce), label in sorted(zip(compactions, labels)):
        if merged and cs < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], ce))
            if ce - cs > longest[-1]:
                longest[-1], merged_labels[-1] = ce - cs, label
        else:
            merged.append((cs, ce))
            merged_labels.append(label)
            longest.append(ce - cs)
    return merged, merged_labels


@dataclass(frozen=True)
class _Record:
    kind: str
    raw: dict[str, Any]
    message: dict[str, Any]
    ts: float


@dataclass
class PhaseTotals:
    model_s: float = 0.0
    tool_s: float = 0.0
    idle_s: float = 0.0
    calls: int = 0

    @property
    def total_s(self) -> float:
        return self.model_s + self.tool_s + self.idle_s


@dataclass
class Analysis:
    totals: dict[Phase, PhaseTotals] = field(default_factory=lambda: defaultdict(PhaseTotals))
    background: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    unmatched_tools: int = 0
    skipped_lines: int = 0
    files: int = 0

    def merge(self, other: Analysis) -> None:
        for phase, t in other.totals.items():
            mine = self.totals[phase]
            mine.model_s += t.model_s
            mine.tool_s += t.tool_s
            mine.idle_s += t.idle_s
            mine.calls += t.calls
        for key, secs in other.background.items():
            self.background[key] += secs
        self.unmatched_tools += other.unmatched_tools
        self.skipped_lines += other.skipped_lines
        self.files += other.files


def _is_human(rec: dict[str, Any], content: object) -> bool:
    origin = rec.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == HUMAN_ORIGIN_KIND
    # Older transcripts carry no origin: a prompt (text, or text/image blocks) that is not a hook (isMeta) is the human's.
    return isinstance(content, (str, list)) and not rec.get("isMeta")


COMPACTION_TRIGGERS = ("auto", "manual")


def _add_compaction(out: Transcript, rec: dict[str, Any]) -> None:
    """Record one ``compact_boundary``: it ends at its timestamp and carries its own duration."""
    meta = rec.get("compactMetadata")
    meta = meta if isinstance(meta, dict) else {}
    try:
        end = epoch_seconds(rec["timestamp"])
    except (ValueError, TypeError, AttributeError, KeyError):
        out.skipped_lines += 1
        return
    duration = meta.get("durationMs")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return
    pre, post = meta.get("preTokens"), meta.get("postTokens")
    trigger = meta.get("trigger")
    shown = trigger if isinstance(trigger, str) and trigger in COMPACTION_TRIGGERS else "?"
    sizes = (
        f", {pre:,} to {post:,} tokens" if isinstance(pre, int) and isinstance(post, int) else ""
    )
    out.compactions.append((end - duration / 1000.0, end))
    out.compaction_labels.append(f"Context compaction ({shown}{sizes})")


def _parse_record(line: str, out: Transcript) -> _Record | None:
    """One transcript line as a message record, or None (counting it in ``skipped_lines`` if it is corrupt)."""
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, RecursionError):
        out.skipped_lines += 1  # half-written live tail, or corruption
        return None
    if (
        isinstance(rec, dict)
        and rec.get("type") == "system"
        and rec.get("subtype") == "compact_boundary"
    ):
        _add_compaction(out, rec)
        return None
    if not isinstance(rec, dict) or rec.get("type") not in RECORD_TYPES:
        return None  # other system / summary / queue records carry no timing we use
    try:
        ts = epoch_seconds(rec["timestamp"])
    except (ValueError, TypeError, AttributeError, KeyError):
        out.skipped_lines += 1  # a message record whose timestamp is missing or malformed
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    return _Record(rec["type"], rec, msg, ts)


def _tool_uses(content: object) -> list[tuple[str, str, dict[str, Any]]]:
    """(tool_use_id, name, input) for each tool_use block of an assistant message."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            raw_input = block.get("input")
            found.append(
                (
                    str(block.get("id")),
                    str(block.get("name")),
                    raw_input if isinstance(raw_input, dict) else {},
                )
            )
    return found


def _add_assistant(out: Transcript, r: _Record) -> None:
    if r.message.get("model") == "<synthetic>":
        return
    mid = str(r.message.get("id") or r.raw.get("uuid"))
    cur = out.messages.setdefault(mid, Message(r.ts, r.ts))
    cur.last_ts = max(cur.last_ts, r.ts)
    cur.text = snippet(r.message.get("content")) or cur.text
    for tool_use_id, name, tool_input in _tool_uses(r.message.get("content")):
        if any(t.tool_use_id == tool_use_id for t in cur.tools):
            continue  # the same message replayed (resumed/forked session)
        cur.tools.append(
            ToolUse(
                tool_use_id,
                classify_tool(name, tool_input),
                bool(tool_input.get("run_in_background")),
                label_tool(name, tool_input),
            )
        )


def _add_user(out: Transcript, r: _Record) -> None:
    if r.raw.get("isCompactSummary"):
        return  # the synthetic summary written by /compact is not a human prompt
    content = r.message.get("content")
    results = (
        [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        if isinstance(content, list)
        else []
    )
    for block in results:
        out.result_ts[str(block.get("tool_use_id"))] = r.ts
    human = False if results else _is_human(r.raw, content)
    out.user_events.append(UserEvent(r.ts, human=human, text=snippet(content) if human else ""))


def read_transcript(path: Path) -> Transcript:
    out = Transcript()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = _parse_record(line, out)
            if record is None:
                continue
            (_add_assistant if record.kind == "assistant" else _add_user)(out, record)
    out.compactions, out.compaction_labels = merge_compactions(
        out.compactions, out.compaction_labels
    )
    return out


@dataclass(frozen=True)
class Interval:
    """One stretch of wall time charged to one phase. ``background`` names the launch an async wait waited on."""

    ts: float
    dur: float
    phase: Phase
    model: bool
    label: str
    background: str = ""


@dataclass(frozen=True)
class Accounting:
    intervals: list[Interval]
    calls: dict[Phase, int]
    unmatched_tools: int


@dataclass
class _Background:
    phase: Phase | None = None
    label: str = ""


def short_duration(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= MINUTES_THRESHOLD_S else f"{seconds:.0f} s"


def _pieces(
    start: float, end: float, compactions: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """(start, length) of the parts of [start, end] not covered by a compaction."""
    out: list[tuple[float, float]] = []
    cur = start
    for cs, ce in compactions:
        if ce <= cur or cs >= end:
            continue
        if cs > cur:
            out.append((cur, cs - cur))
        cur = max(cur, ce)
    if end > cur:
        out.append((cur, end - cur))
    return out


@dataclass(frozen=True)
class _Charge:
    """How to charge a window: its phase and label, and the label used when it turns out to be an idle gap."""

    phase: Phase
    model: bool
    label: str
    idle_note: str  # "{dur}" and "{label}" are filled in
    background: str = ""


def _carved(tr: Transcript, start: float, end: float, charge: _Charge) -> list[Interval]:
    """The window minus compaction time (charged once, as its own interval); idle if still too long."""
    pieces = _pieces(start, end, tr.compactions)
    dur = sum(length for _, length in pieces)
    if dur > IDLE_CUTOFF_S:
        why = charge.idle_note.format(dur=short_duration(dur), label=charge.label)
        return [Interval(ts, length, Phase.IDLE, False, why) for ts, length in pieces]
    return [
        Interval(ts, length, charge.phase, charge.model, charge.label, charge.background)
        for ts, length in pieces
    ]


@dataclass(frozen=True)
class _ToolCharge:
    intervals: list[Interval]
    unmatched: int
    phase: Phase  # of the slowest call: the one the whole message is charged to
    end: float  # when the slowest call's result arrived


def _tool_intervals(
    tr: Transcript, m: Message, start: float | None, floor: float, bg: _Background
) -> _ToolCharge:
    """Model turn (from ``start``) then tool wait (from ``floor``) of a message that called tools."""
    matched = [
        (max(0.0, tr.result_ts[t.tool_use_id] - m.last_ts), t)
        for t in m.tools
        if t.tool_use_id in tr.result_ts
    ]
    wall, slow = max(matched, default=(0.0, m.tools[0]), key=lambda d: d[0])
    more = f" (+{len(m.tools) - 1} in parallel)" if len(m.tools) > 1 else ""
    out: list[Interval] = []
    if start is not None:
        model = _Charge(
            slow.phase,
            True,
            f"Model turn, then {slow.label}{more}",
            "Model turn ran for {dur}: {label}",
        )
        out += _carved(tr, start, m.last_ts, model)
    tool = _Charge(
        slow.phase, False, f"{slow.label}{more}", "Tool call ran or stalled for {dur}: {label}"
    )
    out += _carved(tr, max(m.last_ts, floor), m.last_ts + wall, tool)
    launched = [t for t in m.tools if t.background]
    if launched:
        bg.phase, bg.label = launched[-1].phase, launched[-1].label
    return _ToolCharge(out, len(m.tools) - len(matched), slow.phase, m.last_ts + wall)


_HUMAN_WAIT = _Charge(
    Phase.HUMAN_WAIT,
    False,
    "Waiting for your next prompt",
    "Agent finished and waited {dur} for your next prompt",
)


def _human_wait(before: str, after: str) -> _Charge:
    """The human-wait charge, labelled with what the agent last said and the prompt that ended the wait."""
    ctx = (f"\nAgent said: {before}" if before else "") + (f"\nYou then: {after}" if after else "")
    note = _HUMAN_WAIT.idle_note + ctx.replace("{", "{{").replace("}", "}}")  # idle_note goes through str.format
    return _Charge(_HUMAN_WAIT.phase, _HUMAN_WAIT.model, _HUMAN_WAIT.label + ctx, note)


def _wait_intervals(
    tr: Transcript, m: Message, events: list[UserEvent], bg: _Background, floor: float
) -> tuple[list[Interval], float]:
    """The silence after a tool-less message until the next user event, and where it ended."""
    i = bisect_right(events, m.last_ts, key=lambda e: e.ts)
    if i == len(events):
        return [], m.last_ts
    nxt = events[i]
    begin = max(m.last_ts, floor)
    gap = sum(length for _, length in _pieces(begin, nxt.ts, tr.compactions))
    if gap > IDLE_CUTOFF_S:
        bg.phase, bg.label = None, ""
    if nxt.human:  # a queued prompt does not end a background job
        charge = _human_wait(m.text, nxt.text)
    else:
        idle = "Agent finished; after {dur} a hook or background result resumed it (not a prompt)"
        why = (
            "Waiting on background work: " + bg.label
            if bg.label
            else "Waiting on a background result"
        )
        bg_phase = bg.phase.value if bg.phase is not None else ""
        charge = _Charge(Phase.ASYNC_WAIT, False, why, idle, bg_phase)
    return _carved(tr, begin, nxt.ts, charge), nxt.ts


def _closing_wait(
    tr: Transcript,
    m: Message,
    end: float,
    events: list[UserEvent],
    next_human: list[int | None],
    next_first: float | None,
    floor: float,
) -> tuple[list[Interval], float]:
    """Silence after the last tool result when the human's prompt follows with no further message between."""
    i = bisect_right(events, end, key=lambda e: e.ts)
    j = next_human[i]
    if j is None:
        return [], end
    if next_first is not None and next_first <= events[j].ts:
        return [], end
    charge = _human_wait(m.text, events[j].text)
    return _carved(tr, max(end, floor), events[j].ts, charge), events[j].ts


_THINK = _Charge(
    Phase.THINKING,
    True,
    "Assistant reply, no tool call",
    "Model turn ran for {dur}: {label}",
)


def build_intervals(tr: Transcript) -> Accounting:
    """Every interval charged to a phase. ``analyze`` totals and the timeline both read this, so they cannot differ.

    Compaction time is its own interval and is carved out of whatever model turn, tool wait or silence it overlaps.
    Every window starts no earlier than where the previous message's charged time ended, so nothing is charged twice.
    """
    events = sorted(tr.user_events, key=lambda e: e.ts)
    next_human: list[int | None] = [None] * (
        len(events) + 1
    )  # first human event at or after each index
    for k in range(len(events) - 1, -1, -1):
        next_human[k] = k if events[k].human else next_human[k + 1]
    intervals = [
        Interval(cs, ce - cs, Phase.COMPACTION, False, label)
        for (cs, ce), label in zip(tr.compactions, tr.compaction_labels)
    ]
    calls: dict[Phase, int] = defaultdict(int)
    unmatched = 0
    bg = _Background()
    prev: float | None = None  # the last prompt or tool result before the message
    floor = float("-inf")  # where the previous message's charged time ended
    ev_idx = 0
    messages = list(tr.messages.values())
    for n, m in enumerate(messages):
        while ev_idx < len(events) and events[ev_idx].ts <= m.first_ts:
            prev = events[ev_idx].ts
            ev_idx += 1
        start = None if prev is None else max(prev, floor)
        if m.tools:
            charge = _tool_intervals(tr, m, start, floor, bg)
            intervals += charge.intervals
            unmatched += charge.unmatched
            calls[charge.phase] += len(m.tools)
            next_first = messages[n + 1].first_ts if n + 1 < len(messages) else None
            closing, end = _closing_wait(tr, m, charge.end, events, next_human, next_first, floor)
            intervals += closing
            floor = max(floor, m.last_ts, charge.end, end)
            continue
        calls[Phase.THINKING] += 1
        if start is not None:
            intervals += _carved(tr, start, m.last_ts, _THINK)
        waits, end = _wait_intervals(tr, m, events, bg, floor)
        intervals += waits
        floor = max(floor, m.last_ts, end)
    return Accounting(intervals, dict(calls), unmatched)


_WAIT_PHASES = (Phase.IDLE, Phase.HUMAN_WAIT, Phase.ASYNC_WAIT)


def analyze(path: Path) -> Analysis:
    """Phase totals for one transcript file."""
    tr = read_transcript(path)
    acc = build_intervals(tr)
    res = Analysis(files=1, skipped_lines=tr.skipped_lines, unmatched_tools=acc.unmatched_tools)
    for phase, n in acc.calls.items():
        res.totals[phase].calls += n
    for iv in acc.intervals:
        column = res.totals[iv.phase]
        if iv.phase in _WAIT_PHASES:
            column.idle_s += iv.dur
        elif iv.model:
            column.model_s += iv.dur
        else:
            column.tool_s += iv.dur
        if iv.background:
            res.background[f"{iv.background} (background)"] += iv.dur
    return res


def _files(target: Path, days: int | None) -> list[Path]:
    if target.is_file():
        return [target]
    cutoff = time.time() - days * 86400 if days is not None else 0.0
    return sorted(p for p in target.rglob("*.jsonl") if p.is_file() and p.stat().st_mtime >= cutoff)


def render(title: str, res: Analysis) -> str:
    totals = res.totals
    active = sum(t.total_s for p, t in totals.items() if p not in NOT_ACTIVE)
    human = totals[Phase.HUMAN_WAIT].total_s if Phase.HUMAN_WAIT in totals else 0.0
    idle = totals[Phase.IDLE].total_s if Phase.IDLE in totals else 0.0
    lines = [
        f"== {title}: {res.files} file(s) ==",
        f"{'phase':<22}{'model s':>10}{'tool s':>10}{'idle s':>10}{'total s':>10}{'% active':>10}{'calls':>8}",
    ]
    for phase, t in sorted(totals.items(), key=lambda kv: -kv[1].total_s):
        if phase in NOT_ACTIVE:
            continue
        share = 100.0 * t.total_s / active if active else 0.0
        lines.append(
            f"{phase.value:<22}{t.model_s:>10.0f}{t.tool_s:>10.0f}{t.idle_s:>10.0f}{t.total_s:>10.0f}{share:>9.1f}%{t.calls:>8}"
        )
    lines.append(
        f"active total {active:.0f}s; human wait {human:.0f}s; idle gaps >{IDLE_CUTOFF_S / 60:.0f}min {idle:.0f}s "
        "(last two excluded from % active)"
    )
    if res.background:
        lines.append("async wait attributed to the last background launch:")
        for key, secs in sorted(res.background.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {key:<34}{secs:>8.0f}s")
    lines.append(
        f"skipped {res.skipped_lines} unparseable line(s); {res.unmatched_tools} tool call(s) with no result"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("target", type=Path, help="a transcript .jsonl or a directory of them")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="directory mode: only files modified in the last N days",
    )
    args = parser.parse_args(argv)
    main_sessions, subagents = Analysis(), Analysis()
    for path in _files(args.target, args.days):
        (subagents if SUBAGENT_DIR in path.parts else main_sessions).merge(analyze(path))
    sys.stdout.write(
        "Totals are session-seconds summed over files (concurrent sessions add up); do not add the two sections.\n"
    )
    for title, res in (("main sessions", main_sessions), ("subagents", subagents)):
        if res.files:
            sys.stdout.write(render(title, res) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
