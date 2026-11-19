"""Builds the timeline from the synthetic sample session and checks the numbers it implies."""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "agent-timeline" / "agent_timeline.py"


def build(*extra: str) -> tuple[dict, str]:
    with tempfile.TemporaryDirectory() as d:
        sample, out = Path(d, "s.jsonl"), Path(d, "t.html")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample.py"), str(sample)], check=True, capture_output=True)
        subprocess.run([sys.executable, str(SCRIPT), str(sample), "--out", str(out), *extra], check=True, capture_output=True)
        html = out.read_text()
    return json.loads(re.search(r"const D\s*=\s*(\{.*?\});\s*\n", html, re.S).group(1)), html


class Timeline(unittest.TestCase):
    def test_phase_seconds(self):
        data, _ = build()
        total: dict[str, float] = {}
        for row in data["rows"]:
            total[row[3]] = total.get(row[3], 0) + row[2]
        self.assertAlmostEqual(total["human wait"], 20 * 60 + 20, delta=1)  # the human read the PR for 20m20s
        self.assertAlmostEqual(total["async wait"], 14 * 60, delta=1)  # the background suite ran 14m

    def test_human_wait_carries_context(self):
        data, _ = build()
        label = next(r[6] for r in data["rows"] if r[3] == "human wait")
        self.assertIn("Agent said: The client now retries 5xx", label)
        self.assertIn("You then: Yes, same for the refunds client", label)

    def test_no_detail_drops_every_label(self):
        data, html = build("--no-detail")
        self.assertTrue(all(r[6] == "" for r in data["rows"]))
        self.assertNotIn("Agent said", html)

    def test_thin_blocks_are_hoverable(self):
        _, html = build()
        self.assertIn("function pick(e)", html)  # lane + time hit test, not exact-rect hit test


class Readme(unittest.TestCase):
    def test_local_images_exist(self):
        text = (ROOT / "README.md").read_text()
        for path in re.findall(r'src="https://rivusglobal\.github\.io/agent-timeline/(assets/[^"]+)"', text):
            self.assertTrue((ROOT / "docs" / path).is_file(), path)


class Card(unittest.TestCase):
    def test_card_numbers_and_no_labels(self):
        with tempfile.TemporaryDirectory() as d:
            sample, card = Path(d, "s.jsonl"), Path(d, "c.svg")
            subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample.py"), str(sample)], check=True, capture_output=True)
            subprocess.run([sys.executable, str(SCRIPT), str(sample), "--out", str(Path(d, "t.html")), "--card", str(card)], check=True, capture_output=True)
            svg = card.read_text()
        self.assertIn("41 min of wall clock.", svg)
        self.assertIn("20 min waiting on me.", svg)
        self.assertNotIn("Agent said", svg)
        self.assertNotIn("payments", svg)


class Cursor(unittest.TestCase):
    def test_cursor_files_point_at_the_script(self):
        for rel in (".cursor/commands/agent-timeline.md", ".cursor/rules/agent-timeline.mdc"):
            self.assertIn("agent_timeline.py", (ROOT / rel).read_text(), rel)
        self.assertTrue((SCRIPT).is_file())


if __name__ == "__main__":
    unittest.main()
