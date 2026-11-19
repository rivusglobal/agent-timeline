"""Zoomable phase timeline of one agent session -> self-contained HTML.

Run: ``python agent_timeline.py [transcript] [--out FILE] [--open] [--no-detail] [--card FILE.svg]``.
Supports Claude Code, Codex CLI and OpenCode. Phase accounting is ``phase_report.py`` (the page and
``analyze`` read the same intervals); this module adds subagent lanes and the HTML page.

Each block carries a label taken from the tool call (a command line, a file name, a search pattern; wait blocks also carry a 200-character
snippet of the agent's last message and of the prompt that ended the wait). Labels are
redacted by pattern, which is BEST EFFORT: it misses secrets in unusual shapes. ``--no-detail`` drops every label,
subagent type and subagent description, and is the only guarantee that nothing sensitive reaches the page.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import astuple, dataclass
from pathlib import Path

import phase_report as pr
import adapters

IDLE_DISPLAY_S = 90.0  # long gaps are drawn this wide so they do not swallow the axis
ROUND_DIGITS = 3
OPENCODE_DB = Path.home() / ".local/share/opencode/opencode.db"
TEMPLATE = Path(__file__).with_name("template.html")
PLACEHOLDER = "__DATA__"
AGENTS = ("claude", "codex", "opencode")
SUBAGENT_TYPE_HIDDEN = "subagent"


@dataclass(frozen=True)
class Row:
    """One drawn block. Field order is the array order the page reads."""

    shown_start: float
    shown_dur: float
    real_dur: float
    phase: str
    model: int
    real_start: float
    label: str


@dataclass(frozen=True)
class SubRow:
    start: float
    dur: float
    phase: str
    model: int
    label: str


@dataclass(frozen=True)
class SubagentSpan:
    type: str
    desc: str
    start: float
    end: float
    by: dict[str, float]
    segs: tuple[SubRow, ...]


@dataclass(frozen=True)
class Page:
    span: float
    real_span: float
    rows: tuple[Row, ...]
    session: str
    agent: str
    t0: float
    subs: tuple[SubagentSpan, ...]
    skipped_lines: int


def find_transcript(session: str | None, agent: str | None) -> tuple[str, Path]:
    """(agent, path) of the newest matching session.

    Without ``--session`` Claude sessions come from the current directory's project only; another project's session
    is never picked silently.
    """
    home = Path.home()
    encoded = str(Path.cwd()).replace("/", "-").replace(".", "-")
    cands: list[tuple[str, Path]] = []
    if agent in (None, "claude"):
        claude = (home / ".claude/projects").glob("*/*.jsonl")
        cands += [("claude", p) for p in claude if session or p.parent.name == encoded]
    if agent in (None, "codex"):
        cands += [("codex", p) for p in (home / ".codex/sessions").glob("**/rollout-*.jsonl")]
    if agent in (None, "opencode") and OPENCODE_DB.is_file():
        cands.append(("opencode", OPENCODE_DB))
    if session:
        cands = [(a, p) for a, p in cands if a == "opencode" or session in p.stem]
    if not cands:
        raise SystemExit(
            f"no sessions found for {Path.cwd()}; pass --session <id> or a transcript path "
            "(.jsonl, or opencode.db) and --agent"
        )
    return max(cands, key=lambda c: c[1].stat().st_mtime)


def detect(path: Path) -> str:
    if path.suffix == ".db":
        return "opencode"
    with path.open(encoding="utf-8", errors="replace") as handle:
        head = handle.readline()
    return "codex" if '"payload"' in head else "claude"


def read(agent: str, path: Path, session: str | None) -> pr.Transcript:
    if agent == "opencode":
        return adapters.read_opencode(path, session)
    return adapters.read_codex(path) if agent == "codex" else pr.read_transcript(path)


def segments(tr: pr.Transcript) -> list[pr.Interval]:
    """Every interval ``analyze`` charges. Nothing is dropped: the page skips blips too narrow to see at draw time."""
    return pr.build_intervals(tr).intervals


def _meta_text(meta: dict[str, object], key: str, source: Path) -> str:
    value = meta.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{source}: expected a string {key!r}")
    return value


def _read_meta(meta_file: Path) -> tuple[str, str]:
    """(agentType, description) of a subagent from its ``.meta.json`` sidecar."""
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise TypeError(f"{meta_file}: expected a JSON object")
    return _meta_text(meta, "agentType", meta_file), _meta_text(meta, "description", meta_file)


def _subagent_span(f: Path, t0: float, detail: bool) -> tuple[SubagentSpan, int] | None:
    tr = pr.read_transcript(f)
    segs = segments(tr)
    stamps = [m.first_ts for m in tr.messages.values()] + [m.last_ts for m in tr.messages.values()]
    stamps += [e.ts for e in tr.user_events]
    if not stamps:
        return None
    agent_type, desc = _read_meta(f.with_suffix(".meta.json"))
    by: dict[str, float] = {}
    for s in segs:
        by[s.phase.value] = by.get(s.phase.value, 0.0) + round(s.dur, ROUND_DIGITS)
    end_stamps = [
        *stamps,
        *(s.ts + s.dur for s in segs),
    ]  # whole file span, even with no timed work
    span = SubagentSpan(
        type=pr.redact(agent_type) if detail else SUBAGENT_TYPE_HIDDEN,
        desc=pr.redact(desc) if detail else "",
        start=round(min(stamps) - t0, ROUND_DIGITS),
        end=round(max(end_stamps) - t0, ROUND_DIGITS),
        by={k: round(v, ROUND_DIGITS) for k, v in by.items()},
        segs=tuple(
            SubRow(
                round(s.ts - t0, ROUND_DIGITS),
                round(s.dur, ROUND_DIGITS),
                s.phase.value,
                int(s.model),
                s.label if detail else "",
            )
            for s in segs
        ),
    )
    return span, tr.skipped_lines


def subagents(path: Path, t0: float, detail: bool) -> tuple[tuple[SubagentSpan, ...], int]:
    """One span per Claude Code subagent of this session, and the lines skipped reading them."""
    folder = path.parent / path.stem / pr.SUBAGENT_DIR
    files = sorted(folder.glob("*.jsonl")) if folder.is_dir() else []
    found = [r for r in (_subagent_span(f, t0, detail) for f in files) if r is not None]
    spans = tuple(sorted((s for s, _ in found), key=lambda s: s.start))
    return spans, sum(skipped for _, skipped in found)


def build(agent: str, path: Path, session: str | None, detail: bool = True) -> Page:
    """The page's data: idle gaps drawn ``IDLE_DISPLAY_S`` wide at most, real times kept per row."""
    tr = read(agent, path, session)
    segs = sorted(segments(tr), key=lambda s: s.ts)
    if not segs:
        raise SystemExit("no timed activity in this transcript")
    t0 = segs[0].ts
    rows: list[Row] = []
    shift = 0.0  # real seconds removed by compressing idle gaps
    for s in segs:
        start = s.ts - t0
        shown = IDLE_DISPLAY_S if s.phase is pr.Phase.IDLE and s.dur > IDLE_DISPLAY_S else s.dur
        label = s.label if detail else ""
        rows.append(
            Row(
                round(start - shift, ROUND_DIGITS),
                round(shown, ROUND_DIGITS),
                round(s.dur, ROUND_DIGITS),
                s.phase.value,
                int(s.model),
                round(start, ROUND_DIGITS),
                label,
            )
        )
        shift += s.dur - shown
    subs, sub_skipped = subagents(path, t0, detail) if agent == "claude" else ((), 0)
    return Page(
        span=max(r.shown_start + r.shown_dur for r in rows),
        real_span=max(r.real_start + r.real_dur for r in rows),
        rows=tuple(rows),
        session=path.stem[:8],
        agent=agent,
        t0=t0,
        subs=subs,
        skipped_lines=tr.skipped_lines + sub_skipped,
    )


def page_json(page: Page) -> dict[str, object]:
    """The page as the JSON object the template reads (rows are arrays; the field order is the contract)."""
    return {
        "span": page.span,
        "real_span": page.real_span,
        "rows": [list(astuple(r)) for r in page.rows],
        "session": page.session,
        "agent": page.agent,
        "t0": page.t0,
        "not_active": [p.value for p in pr.NOT_ACTIVE],
        "subs": [
            {
                "type": s.type,
                "desc": s.desc,
                "start": s.start,
                "end": s.end,
                "by": s.by,
                "segs": [list(astuple(g)) for g in s.segs],
            }
            for s in page.subs
        ],
    }


_SCRIPT_ESCAPES = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
    (" ", "\\u2028"),
    (" ", "\\u2029"),
)


def embed_json(page: Page) -> str:
    """JSON safe inside a <script> element: log text cannot close it or open a comment (valid JSON escapes only)."""
    text = json.dumps(page_json(page))
    for char, escape in _SCRIPT_ESCAPES:
        text = text.replace(char, escape)
    return text


def render_html(page: Page) -> str:
    return TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, embed_json(page))


def _write_page(html: str, out: Path | None) -> Path:
    """Write to ``out``, or to a fresh private temp file (0600, never an existing or symlinked path)."""
    if out is not None:
        out.write_text(html, encoding="utf-8")
        return out
    fd, name = tempfile.mkstemp(prefix="agent-timeline-", suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(html)
    return Path(name)


CARD_PHASES = ("human wait", "async wait", "waiting/polling", "local commands")


def _hours(sec: float) -> str:
    return f"{sec / 3600:.1f} h" if sec >= 3600 else f"{sec / 60:.0f} min"


def render_card(page: Page) -> str:
    """A 1200x630 SVG share card: wall clock plus where the biggest waits went. Numbers only, no labels."""
    tot: dict[str, float] = {}
    for r in page.rows:
        tot[r.phase] = tot.get(r.phase, 0.0) + r.real_dur
    wait = tot.get("human wait", 0.0)
    lines = [(p, tot[p]) for p in CARD_PHASES if tot.get(p, 0.0) > 0][:4]
    bars = "".join(
        f'<text x="80" y="{360 + i * 62}" font-size="30" fill="#5b574c">{p}</text>'
        f'<rect x="360" y="{338 + i * 62}" width="{max(6, 700 * v / page.real_span):.0f}" height="30" fill="#16140f"/>'
        f'<text x="{360 + max(6, 700 * v / page.real_span) + 14:.0f}" y="{362 + i * 62}" font-size="30" fill="#16140f">{_hours(v)}</text>'
        for i, (p, v) in enumerate(lines)
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630" width="1200" height="630" '
        'font-family="Georgia, serif">'
        '<rect width="1200" height="630" fill="#f4f1ea"/>'
        f'<text x="80" y="150" font-size="76" fill="#16140f">{_hours(page.real_span)} of wall clock.</text>'
        f'<text x="80" y="238" font-size="76" font-style="italic" fill="#16140f">{_hours(wait)} waiting on me.</text>'
        f'<g font-family="Helvetica, Arial, sans-serif">{bars}</g>'
        '</svg>'
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("transcript", nargs="?", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument(
        "--session", help="session id (prefix) to pick; for opencode the full session id"
    )
    ap.add_argument("--agent", choices=AGENTS, help="default: detect")
    ap.add_argument(
        "--no-detail",
        action="store_true",
        help="drop labels, subagent types and descriptions (the only way to guarantee nothing sensitive is shown)",
    )
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--card", type=Path, metavar="FILE.svg", help="also write a 1200x630 share card (numbers only, no labels)")
    a = ap.parse_args(argv)
    if a.transcript:
        agent, path = a.agent or detect(a.transcript), a.transcript
    else:
        agent, path = find_transcript(a.session, a.agent)
    sys.stderr.write(f"agent-timeline: reading {agent} session {path}\n")
    page = build(agent, path, a.session if agent == "opencode" else None, detail=not a.no_detail)
    if page.skipped_lines:
        sys.stderr.write(f"agent-timeline: skipped {page.skipped_lines} unparseable line(s)\n")
    out = _write_page(render_html(page), a.out)
    if a.card:
        a.card.write_text(render_card(page), encoding="utf-8")
        sys.stdout.write(f"{a.card}\n")
    sys.stdout.write(f"{out}\n")
    if a.open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, str(out.resolve())], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
