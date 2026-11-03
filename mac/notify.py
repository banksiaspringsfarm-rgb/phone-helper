"""The kit's small stand-in for alerts: phone.py writes events here and calls auth_failed on every refusal.

Everything is written to ~/.phone-helper/activity.jsonl (600). send() only prints: wire it to your own
alert channel (Telegram bot, email, ntfy) if you want to hear about refused requests on your phone.
"""
import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("PHONE_HELPER_DIR") or Path.home() / ".phone-helper")
ACTIVITY = ROOT / "activity.jsonl"
KNOWN = ROOT / "known.json"
AUTH_FAIL_ALERT = 5          # this many refused requests ...
BURST_WINDOW_MIN = 10        # ... within this many minutes → one alert
_lock = threading.Lock()


def record(kind: str, **fields) -> dict:
    ev = {"t": time.time(), "kind": kind, **fields}
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock:
        new = not ACTIVITY.exists()
        with ACTIVITY.open("a") as f:
            f.write(json.dumps(ev) + "\n")
        if new:
            ACTIVITY.chmod(0o600)
    return ev


def events(since: float) -> list[dict]:
    if not ACTIVITY.exists():
        return []
    out = []
    for line in ACTIVITY.read_text().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("t", 0) >= since:
            out.append(ev)
    return out


def send(text: str) -> bool:
    """Your alert channel. The kit only prints; replace this to get a message on your phone."""
    print(f"ALERT: {text}", flush=True)
    return True


def auth_failed(ip: str, why: str):
    record("auth_fail", ip=ip, why=why)
    n = sum(1 for e in events(time.time() - BURST_WINDOW_MIN * 60) if e["kind"] == "auth_fail")
    if n >= AUTH_FAIL_ALERT:
        send(f"phone helper: {n} refused requests in {BURST_WINDOW_MIN} min, latest from {ip} ({why}).")
