"""Writes examples/sample-session.jsonl: a made-up Claude Code session with known timings.

Nothing here comes from a real session. The tests assert the phase totals this script implies.
"""

import json
import sys
from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 11, 10, 9, 0, 0, tzinfo=timezone.utc)
recs: list[dict] = []
n = 0


def ts(sec: float) -> str:
    return (T0 + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")


def human(sec: float, text: str) -> None:
    recs.append({"type": "user", "timestamp": ts(sec), "origin": {"kind": "human"},
                 "message": {"role": "user", "content": text}})


def notify(sec: float) -> None:
    recs.append({"type": "user", "timestamp": ts(sec), "origin": {"kind": "task-notification"},
                 "message": {"role": "user", "content": "background job finished"}})


def call(sec: float, done: float, name: str, tool_input: dict, text: str = "") -> None:
    """One assistant message with one tool call, sent at `sec`, answered at `done`."""
    global n
    n += 1
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": f"t{n}", "name": name, "input": tool_input}]
    recs.append({"type": "assistant", "timestamp": ts(sec), "uuid": f"u{n}",
                 "message": {"id": f"m{n}", "role": "assistant", "model": "sample", "content": content}})
    recs.append({"type": "user", "timestamp": ts(done),
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": "ok"}]}})


def say(sec: float, text: str) -> None:
    global n
    n += 1
    recs.append({"type": "assistant", "timestamp": ts(sec), "uuid": f"u{n}",
                 "message": {"id": f"m{n}", "role": "assistant", "model": "sample",
                             "content": [{"type": "text", "text": text}]}})


human(0, "Add retry with backoff to the payments client")
# a burst of short reads and greps: these are the hairline blocks
t = 8.0
for i, (tool, inp) in enumerate([
    ("Read", {"file_path": "/repo/payments/client.py"}),
    ("Grep", {"pattern": "requests.post"}),
    ("Read", {"file_path": "/repo/payments/errors.py"}),
    ("Grep", {"pattern": "class .*Error"}),
    ("Read", {"file_path": "/repo/tests/payments/test_client.py"}),
    ("Glob", {"pattern": "payments/**/*.py"}),
]):
    call(t, t + 1.2, tool, inp)
    t += 3.0
call(t + 10, t + 11, "Edit", {"file_path": "/repo/payments/client.py", "old_string": "a", "new_string": "b"})
call(t + 40, t + 42, "Edit", {"file_path": "/repo/tests/payments/test_client.py", "old_string": "a", "new_string": "b"})
call(t + 55, t + 175, "Bash", {"command": "make test-payments", "description": "Run the payments tests"})
call(t + 190, t + 192, "Bash", {"command": "git commit -am 'retry with backoff'", "description": "Commit the change"})
call(t + 200, t + 205, "Bash", {"command": "gh pr create --fill", "description": "Open the pull request"})
call(t + 215, t + 275, "Bash", {"command": "sleep 55; gh pr checks", "description": "Wait for CI checks"})
say(t + 290, "The client now retries 5xx responses three times with exponential backoff. PR is open and CI is green. Want me to do the refunds client too?")
# the human reads the PR for ~20 minutes
h2 = t + 290 + 20 * 60 + 20
human(h2, "Yes, same for the refunds client, and run the full suite in the background")
call(h2 + 14, h2 + 16, "Read", {"file_path": "/repo/refunds/client.py"})
call(h2 + 30, h2 + 33, "Edit", {"file_path": "/repo/refunds/client.py", "old_string": "a", "new_string": "b"})
call(h2 + 45, h2 + 46, "Bash", {"command": "make test", "description": "Run the full test suite", "run_in_background": True})
say(h2 + 55, "Full suite is running in the background; I will check when it finishes.")
notify(h2 + 55 + 14 * 60)
call(h2 + 55 + 14 * 60 + 9, h2 + 55 + 14 * 60 + 12, "Bash", {"command": "git commit -am 'retry refunds'", "description": "Commit the refunds change"})
say(h2 + 55 + 14 * 60 + 25, "Refunds client done, full suite green, committed.")

out = sys.argv[1] if len(sys.argv) > 1 else "examples/sample-session.jsonl"
with open(out, "w", encoding="utf-8") as f:
    for r in recs:
        f.write(json.dumps(r) + "\n")
print(f"wrote {out}: {len(recs)} records")
