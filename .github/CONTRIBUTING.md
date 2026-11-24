# Contributing

Small, focused changes are easiest to merge. Run `python -m unittest discover -s tests -v` before you open a PR; it must pass.

## Add an agent

Write `read_<agent>(path) -> phase_report._Transcript` in `skills/agent-timeline/adapters.py`: assistant turns (`_Message` with first and last timestamp and a `_ToolUse` list), `result_ts[tool_call_id]`, and `_UserEvent`s. Route tool names through `adapters.normalize`. Register the agent in `find_transcript`, `detect` and `read` in `agent_timeline.py`, and add a fixture test with known phase seconds, as `tests/test_timeline.py` does for the sample session.

## Changing the numbers

`phase_report.py` owns the phase accounting and `template.html` has a "How these numbers are computed" section. Change both together.

## Privacy

Any new text shown on the page must go through `phase_report.redact` and be dropped by `--no-detail`. A test must show both.

## The sample session

`examples/sample-session.jsonl` and `docs/demo.html` are made up by `scripts/make_sample.py`. Never put a real session in this repo (no transcripts or pages). `docs/assets/real-session.png` is a cropped screenshot of the maintainers' own session, with task titles and paths cropped out.

## Logo, banner and demo GIF

`assets/banner.png` (1280x640) is rendered from `scripts/banner.html` and is the one to upload as the repo's social preview. `assets/demo.gif`, `hero.png` and `hover.png` are captured from `docs/demo.html`, so regenerate them if the page changes.
