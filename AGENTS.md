# Notes for coding agents

This repo is a self-hosted phone helper: an Android app (`android/`) and a computer-side server plus MCP voice
tools (`mac/`). If the person wants to install it, follow `skills/phone-helper-setup/SKILL.md` step by step.

- Tests: `cd mac && python3 -m unittest -q test_phone test_kit_server`; `cd android && ./gradlew testDebugUnitTest`.
- Keep every safety rule in SKILL.md ("The safety rules"). Never weaken a number rule, the read-back and yes,
  the one-use tokens or the rate limits without the owner asking for that exact change.
- Never connect it to any server other than the owner's own computer, and never expose it to the internet.
