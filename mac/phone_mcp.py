"""The voice tools: an MCP server your Claude uses to find a contact, call, text, and hear new texts and missed calls.

    python3 phone_mcp.py                      stdio (Claude Code, Claude Desktop, Cursor, Copilot: add it as a local
                                              MCP server, see the README)
    python3 phone_mcp.py --http --port 8771   streamable HTTP on 127.0.0.1, for putting behind your own authenticated
                                              connector (the Claude phone app's voice mode only loads remote connectors)

The safety rules live in phone.py and on the phone, not in these descriptions: a one-use 60 s token from a lookup,
saved contacts only, emergency/premium/short numbers refused, rate limits, one order on its way at a time.
Needs: pip install mcp
"""
import argparse
import sys

from mcp.server.fastmcp import FastMCP

import phone as PH

mcp = FastMCP("phone-helper")


@mcp.tool()
def phone_find_contact(name: str, action: str = "call", body: str = "") -> str:
    """"Call Sam" / "text Sam I'm running late": look a saved contact up on the user's phone (action "call", or
    "text" with body = the text, at most 300 characters). Only name, kind of number and its last 3 digits come back,
    each one allowed with a one-use token good for 60 s. Read back exactly what it says (for a text, the words too)
    and WAIT for the user's clear yes before phone_call or phone_text. Saved contacts only, never a spoken number."""
    return PH.find_contact(name, action, body)


@mcp.tool()
def phone_call(token: str) -> str:
    """Ring the contact phone_find_contact gave this token for, ONLY after the user said yes to the read-back. The
    phone dials a few seconds after any Claude voice call ends, so then tell them to hang up. Other open tokens die."""
    return PH.call(token)


@mcp.tool()
def phone_text(token: str, body: str) -> str:
    """Text the contact phone_find_contact gave this token for, ONLY after the user said yes to the read-back of the
    name, number ending and words. body must be the same text, word for word, else it is refused. Then tell them
    what it says to do; it sends a few seconds after any Claude voice call ends. Other open tokens die."""
    return PH.text(token, body)


@mcp.tool()
def phone_inbox(recent: bool = False, show_codes: bool = False) -> str:
    """"Read my texts" / "who rang?": new texts and missed calls on the user's phone, oldest first, then marked heard.
    Read them ONE AT A TIME. recent=True also gives the ones heard in the last day. A one-time security code is held
    back: show_codes=True ONLY when the user asks for the code. To reply, phone_find_contact(action="text")."""
    return PH.inbox(recent=recent, show_codes=show_codes)


@mcp.tool()
def phone_rundown() -> str:
    """Call at the start of a conversation: what happened to earlier calls and texts (sent, delivered, cancelled,
    refused) and who texted or rang since, each said once. Empty when there is nothing new."""
    lines = PH.call_start_lines() + PH.inbox_call_start_lines()
    return "\n".join(lines) or "Nothing new from the phone."


@mcp.tool()
def phone_status() -> str:
    """Is the phone helper connected, and when was it last seen? Use when asked, or before promising a call if a
    phone tool said the phone isn't connected."""
    return PH.status()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="phone helper voice tools (MCP)")
    ap.add_argument("--http", action="store_true", help="streamable HTTP on 127.0.0.1 instead of stdio")
    ap.add_argument("--port", type=int, default=8771)
    a = ap.parse_args(argv)
    if a.http:
        mcp.settings.host, mcp.settings.port = "127.0.0.1", a.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
