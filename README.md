# Phone helper

Let your own Claude ring and text your saved contacts from your Android phone, and read you new texts and
missed calls. You say "text Sam I'm running late", Claude reads back the name, the last three digits of the
number and the words, you say yes, and a few seconds later your phone sends it as a normal text from your own
number. Calls work the same way and ring once the Claude conversation goes quiet.

It is self-hosted and free for personal use. Your phone keeps one connection out to **your own computer** over
**your own Tailscale network**; nothing goes through anyone else's server.

## Set it up with Claude
Open Claude Code (or Cursor, Copilot, any agent that reads `SKILL.md` skills) in this folder and say:

> Set up the phone helper for me.

The skill in `skills/phone-helper-setup/SKILL.md` walks your Claude through it: checking your phone and
computer, building the Android app with your computer's address, installing it, pairing it, adding the voice
tools, and a safe first test. As a Claude Code plugin: `/plugin marketplace add banksiaspringsfarm-rgb/phone-helper`,
then `/plugin install phone-helper`.

## What's in it
- `android/`: the app (Kotlin, no third-party libraries). Unit tests for the number and text rules.
- `mac/`: the computer side, Python standard library: `phone_server.py` (the phone's connection, tailnet only),
  `phone.py` (pairing, one-use tokens, rate limits, inbox, the log), `phone_mcp.py` (the voice tools, MCP),
  tests.
- `docs/HOW-IT-WORKS.md`: the design in one page.

## Safety, built in
- A read-back and your clear yes before every call or text; each lookup gives a token that works once, for 60 s.
- Saved contacts only, never a spoken number. Emergency, premium and symbol numbers are refused on the phone,
  checked again at the moment it dials. 10 calls or texts in 10 minutes, 100 a day.
- Full numbers never leave the phone; texts' words are kept on your computer only (7 days) and never logged.
  One-time security codes are held back unless you ask.
- Debug builds never really dial or send on a real phone. The server only listens on 127.0.0.1 and only
  answers your own Tailscale login.

The number rules are written for Australia (000, 112, 106, 190x, 0055). Elsewhere, ask your Claude to adapt
`NumberRules.kt` for your country before the first real call; the phone's own emergency-number list is
already used everywhere.

## Licence
PolyForm Noncommercial 1.0.0: free for personal and non-commercial use; commercial use needs permission (open an
issue). See `LICENSE.md`. No warranty: it places real calls and sends real texts from your phone.
