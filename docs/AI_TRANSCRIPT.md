# AI coding-assistant transcript

This project was built with **Claude Code** (Claude Opus 5.5) in a single session.

To include the full transcript, export the Claude Code session that built this repo (for example `/export` in Claude Code, or copy it from `~/.claude/projects/…/<session>.jsonl`) and save it next to this file as `docs/ai_transcript.md` or `.jsonl`.

The session went roughly like this:
1. Read the assignment PDF and listed the requirements.
2. Designed the architecture: a single agent where code enforces approvals, idempotency and memory gating.
3. Built the mocks → store → memory → workflow → approval → executor → context → agent → scripted model, then the eval harness.
4. The first eval run passed 28/35. Fixing the failures surfaced the real bugs written up in `docs/BUGS.md`. Each now has a regression test.
5. Added the live-adapter test (real SDK, mocked HTTP), the demo web UI, and the docs.
