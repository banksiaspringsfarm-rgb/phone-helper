"""Phone order channel: the Mac side of the user's phone-helper app (dial and text saved contacts for voice Claude).

the user, 2026-11-02 (D194/D195; design: docs/HOW-IT-WORKS.md §2, §8). The phone-helper app on their
Motorola keeps ONE long-poll open to the approvals daemon under /phone (tailnet HTTPS via `tailscale serve`); the
connector and the daemon share a file store. The phone always calls the Mac; the Mac never needs adb.

    POST /phone/api/pair          {"code", "name", "app_version"}   → {"device", "key"}   (one-time 8-digit code)
    GET  /phone/api/next?wait=25                                      → 200 <order> | 204 after wait s
    POST /phone/api/result        <result>                            → {"ok": true}       (409 unknown/answered)
    POST /phone/api/inbox         {"items": [<sms | missed call>]}    → {"ok": true, "new": n}  (phone-helper 0.2.3+)

Every request needs, in this order: the user's tailnet identity (Tailscale-User-Login == tailnet_login; FAILS CLOSED
when tailnet_login is empty) → 403; then for next/result the device key (Authorization: Bearer <key> and
X-Phone-Device: <id>, sha256 compared, not revoked) → 401; then size (≤ 64 KB, refused before reading) → 413 and
shape → 400. Every refusal is one phone-log line plus notify.auth_failed.

Inbox (the user, 4 Nov: the phone sends new incoming texts and missed calls; voice Claude
hears them in call_start and reads them with phone_inbox. The sender is a saved name or the last 3 digits only. An
incoming text's words are the one other thing allowed digits: kept only in inbox.json (7 days), never logged.

Never in the store or log: a full number (a result with 6+ digits in a row is refused), the device key, a pairing
code or a token (only hashes are kept).

    phone.py pair --name my-phone [--test]     print a one-time pairing code (15 min, one use)
    phone.py devices                                paired devices and when each was last seen
    phone.py revoke <device>                        end a device at once
    phone.py send <kind> [--name Lena] [--action call] [--arg k=v] [--device d_..] [--wait] [--timeout s]
                                                    queue an order (testing); --wait prints the phone's result,
                                                    exit 0 ok / 1 not ok / 2 no answer
    phone.py log [-n 20]                            the phone log, newest last
    phone.py outcomes [--peek]                      calls not gone through, as call_start says them (once)
    phone.py prune                                  drop orders and results older than 7 days

Voice tools (server.py phone_find_contact / phone_call / phone_text → find_contact() / call() / text()): design §4
tokens, §11 texts, see below. A text's body is the one thing allowed digits (it is the user's own words, read back to
them) and lives only in its open order file: replaced by body_len the moment the phone takes it or its life ends.

Store: ~/.phone-helper/ (dir 700, files 600), or $PHONE_HELPER_DIR (tests, the emulator harness).
Standard library only, like the rest of the connector.
"""
import argparse
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import notify as N

DEFAULT_DIR = Path.home() / ".phone-helper"
DIR = Path(os.environ.get("PHONE_HELPER_DIR") or DEFAULT_DIR)

MAX_BODY = 64 * 1024
MAX_WAIT_S = 25
POLL_S = 0.25
CODE_LIFE_S = 15 * 60
CODE_MAX_LIFE_S = 12 * 3600                                 # `pair --hours`: a code put on the install page (sideload)
CODE_MAX_WRONG = 5
LIFE_S = {"ping": 10, "find": 10, "call": 90, "text": 90}    # order life by kind; anything else 30 s
MIN_LEFT_S = {"call": 70, "text": 70}                       # never deliver with less left (the 60 s wait must fit)
KEEP_DAYS = 7
OUTCOMES = ("done", "dialled", "sent", "refused", "cancelled", "failed", "unsupported")
ORDER_ID = re.compile(r"^o_[A-Za-z]{4,32}\Z")                # the Mac makes letters-only ids (_new_id)
DEVICE_ID = re.compile(r"^d_[A-Za-z]{4,32}\Z")
NUMBER_LIKE = re.compile(r"\d(?:[\s()./,_+-]*\d){5,}")       # 6+ digits in a row, spacing allowed: a phone number
OPAQUE_ID = re.compile(r"^[a-z]_[A-Za-z]{1,32}\Z|^[a-z]_[A-Za-z0-9]{1,5}\Z")   # ids: letters, or too short to hide a number
MAX_DEPTH = 8                                               # a result nests 4 deep; deeper is refused, never recursed


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _new_id(prefix: str) -> str:
    """Letters only, so an id can never look like a phone number to the number check."""
    return prefix + "".join(secrets.choice("abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(10))


# ── store ──────────────────────────────────────────────────────────────────────

def _ensure() -> Path:
    for d in (DIR, DIR / "orders", DIR / "results"):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    return DIR


@contextlib.contextmanager
def _lock():
    _ensure()
    with os.fdopen(os.open(DIR / "lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write(path: Path, obj):
    _ensure()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def log(ev: str, **fields) -> dict:
    """One JSON line in phone.log (design §8) plus one in notify's activity log."""
    line = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "ev": ev, **fields}
    _ensure()
    fd = os.open(DIR / "phone.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(line) + "\n")
    N.record("phone", **{("order_kind" if k == "kind" else k): v for k, v in line.items() if k != "at"})
    return line


def read_log(n: int = 20) -> list[dict]:
    try:
        lines = (DIR / "phone.log").read_text().splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(x) for x in lines[-n:] if x.strip()]


# ── devices and pairing ────────────────────────────────────────────────────────

def devices() -> dict:
    return _read(DIR / "devices.json", {})


def pair_code(name: str, test: bool = False, life_s: float = CODE_LIFE_S) -> str:
    """A one-time 8-digit code, 15 min unless asked longer (at most 12 h); only its sha256 is stored."""
    code = f"{secrets.randbelow(10 ** 8):08d}"
    now = time.time()
    life_s = max(60.0, min(float(life_s), CODE_MAX_LIFE_S))
    with _lock():
        p = _read(DIR / "pairing.json", {"codes": [], "wrong": 0})
        p["codes"] = [c for c in p["codes"] if c["expires"] > now]
        p["codes"].append({"sha256": _sha(code), "name": name, "test": test, "expires": now + life_s})
        _write(DIR / "pairing.json", p)
    log("pair_code", name=name, test=test)
    return code


def pair(code: str, name: str = "", app_version: str = "") -> tuple[str, str] | None:
    """Trade a pairing code for (device id, key). None if the code is wrong, used or expired.
    Five wrong tries against open codes kill every open code."""
    now = time.time()
    with _lock():
        p = _read(DIR / "pairing.json", {"codes": [], "wrong": 0})
        p["codes"] = [c for c in p["codes"] if c["expires"] > now]
        got = next((c for c in p["codes"] if hmac.compare_digest(c["sha256"], _sha(str(code)))), None)
        if not got:
            if p["codes"]:
                p["wrong"] = p.get("wrong", 0) + 1
                if p["wrong"] >= CODE_MAX_WRONG:
                    p = {"codes": [], "wrong": 0}
            _write(DIR / "pairing.json", p)
            return None
        p["codes"].remove(got)
        p["wrong"] = 0
        _write(DIR / "pairing.json", p)
        name = got["name"] or name or "phone"
        devs = {d: v for d, v in devices().items() if v.get("name") != name}    # pairing again replaces the old key
        device, key = _new_id("d_"), secrets.token_urlsafe(32)
        devs[device] = {"name": name, "key_sha256": _sha(key), "paired": now, "last_seen": 0,
                        "app_version": str(app_version)[:20], "net": "", "revoked": False, "test": got["test"]}
        _write(DIR / "devices.json", devs)
    log("pair", device=device, name=name, app_version=str(app_version)[:20])
    return device, key


def revoke(device: str) -> bool:
    with _lock():
        devs = devices()
        if device not in devs:
            return False
        devs[device]["revoked"] = True
        _write(DIR / "devices.json", devs)
    log("revoke", device=device)
    return True


def authenticate(device: str, key: str) -> bool:
    d = devices().get(device) if device else None
    want = d["key_sha256"] if d else _sha("no such device")
    ok = hmac.compare_digest(want, _sha(key or ""))
    return bool(d and ok and key and not d.get("revoked"))


def touch(device: str, app_version: str = "", net: str = "", reads: str = ""):
    """Every /next: when the phone was last seen, its app version, network (phone_status reads it) and what it may
    read for the inbox ("sms,calls", "sms", "calls" or "none"; 0.2.3 on)."""
    with _lock():
        devs = devices()
        if device not in devs:
            return
        devs[device]["last_seen"] = time.time()
        if app_version:
            devs[device]["app_version"] = app_version[:20]
        if net:
            devs[device]["net"] = net[:12]
        if reads in INBOX_READS:
            devs[device]["reads"] = reads
        _write(DIR / "devices.json", devs)


def default_device() -> str | None:
    """The active device: the only one, or the one seen most recently."""
    live = [(v.get("last_seen", 0), v.get("paired", 0), d) for d, v in devices().items() if not v.get("revoked")]
    return max(live)[2] if live else None


# ── orders and results ─────────────────────────────────────────────────────────

def _order_path(oid: str) -> Path:
    return DIR / "orders" / f"{oid}.json"


def _result_path(oid: str) -> Path:
    return DIR / "results" / f"{oid}.json"


def load_order(oid: str) -> dict | None:
    return _read(_order_path(oid), None) if ORDER_ID.match(oid or "") else None


def send(kind: str, args: dict | None = None, device: str | None = None, life_s: float | None = None) -> dict:
    """Queue an order for the phone (the connector's side; it never talks HTTP)."""
    order = _make_order(kind, args, device, life_s)
    with _lock():
        _write(_order_path(order["id"]), order)
    return order


def _make_order(kind: str, args: dict | None, device: str | None, life_s: float | None) -> dict:
    device = device or default_device()
    if not device:
        raise LookupError("no paired phone")
    checked = {k: v for k, v in (args or {}).items() if k != "body"}     # a text's body may hold digits (§11.3)
    if any(NUMBER_LIKE.search(x) for x in (_flatten({"args": checked}) or ["0" * 6])):
        raise ValueError("an order never carries a full number")   # the store holds no numbers, either way
    now = time.time()
    life = life_s if life_s is not None else LIFE_S.get(kind, 30)
    return {"v": 1, "id": _new_id("o_"), "kind": kind, "device": device, "created": now, "expires": now + life,
            "args": args or {}, "state": "open"}


def _wire(order: dict, now: float) -> dict:
    """What the phone gets: no state fields, and ttl_s relative so it never trusts the Mac's clock."""
    out = {k: order[k] for k in ("v", "id", "kind", "device", "created", "expires")}
    out["args"] = dict(order["args"])                         # a copy: the stored order is scrubbed after this
    out["ttl_s"] = round(order["expires"] - now, 1)
    return out


def _scrub(o: dict) -> dict:
    """A text's body leaves the store: only its length stays (design §11.3)."""
    args = o.get("args")
    if isinstance(args, dict) and "body" in args:
        args["body_len"] = len(str(args.pop("body")))
    return o


def scrub_stale(now: float | None = None) -> int:
    """Mark every open or delivered order past its life expired, scrubbing a text's body, so no body outlives its
    order even when the phone never asks again (run by the voice tools, call_start's outcomes, prune and the daemon
    start). Returns how many it ended."""
    now = now or time.time()
    gone = []
    with _lock():
        for f in (DIR / "orders").glob("o_*.json"):
            o = _read(f, None)
            if not isinstance(o, dict) or o.get("state") not in ("open", "delivered"):
                continue
            if o.get("expires", 0) <= now or (o.get("state") == "delivered" and "body" in o.get("args", {})):
                if o.get("expires", 0) <= now:
                    o["state"] = "expired"
                    gone.append(o)
                _write(f, _scrub(o))
    for o in gone:
        log("expired", device=o.get("device", ""), order=o.get("id", ""), kind=o.get("kind", ""),
            delivered="delivered" in o)
    return len(gone)


def next_order(device: str) -> dict | None:
    """The oldest open order for this device, marked delivered (once). Orders too close to expiry are skipped.
    A text's body goes to the phone and is scrubbed from the store in the same step (design §11.3)."""
    now = time.time()
    with _lock():
        best = None
        if devices().get(device, {}).get("revoked", True):
            return None
        for f in (DIR / "orders").glob("o_*.json"):
            o = _read(f, None)
            if not isinstance(o, dict) or o.get("device") != device or o.get("state") != "open":
                continue
            if not all(k in o for k in ("v", "id", "kind", "created", "expires", "args")):
                continue
            if o["expires"] <= now:
                o["state"] = "expired"
                _write(f, _scrub(o))
                log("expired", device=device, order=o["id"], kind=o["kind"], delivered=False)
                continue
            if o["expires"] - now < MIN_LEFT_S.get(o["kind"], 5):
                continue
            if best is None or o["created"] < best["created"]:
                best = o
        if not best:
            return None
        best.update(state="delivered", delivered=now)
        wire = _wire(best, now)
        _write(_order_path(best["id"]), _scrub(best))
    return wire


_polls: dict[str, int] = {}          # device → newest long-poll; an older one (a dead mobile link) gives way
_polls_lock = threading.Lock()


def wait_next(device: str, wait_s: float, gone=lambda: False) -> dict | None:
    """Long-poll for this device. A newer poll from the same device ends this one with nothing, so a connection
    that died quietly can never swallow an order the phone's fresh connection should get. gone() is True once the
    phone has closed this connection (its app was killed by an update, design §2.4): the poll ends without taking
    an order, which then waits for the restarted app instead of going to a dead socket."""
    with _polls_lock:
        me = _polls[device] = _polls.get(device, 0) + 1
    end = time.time() + max(0.0, min(wait_s, MAX_WAIT_S))
    while True:
        with _polls_lock:
            if _polls.get(device) != me or gone():
                return None
            o = next_order(device)          # under the poll lock, so a superseded poll cannot take it
        if o or time.time() >= end:
            return o
        time.sleep(POLL_S)


def check_result(res) -> str:
    """'' if the result has the expected shape, else why not."""
    if not isinstance(res, dict):
        return "result must be an object"
    if not ORDER_ID.match(str(res.get("id", ""))):
        return "bad id"
    if not isinstance(res.get("ok"), bool):
        return "ok must be true or false"
    if res.get("outcome") not in OUTCOMES:
        return "unknown outcome"
    if res.get("data") is not None and not isinstance(res["data"], dict):
        return "data must be an object"
    m = (res.get("data") or {}).get("matches")
    if m is not None and not (isinstance(m, list) and all(isinstance(x, dict) for x in m)):
        return "matches must be a list of objects"
    flat = _flatten(res)
    if flat is None:
        return "nested too deep"
    if any(NUMBER_LIKE.search(x) for x in flat):
        return "looks like a phone number: the Mac never takes full numbers"
    if not all(OPAQUE_ID.match(x) for x in _ids(res.get("data"))):
        return "bad find or pick id"
    return ""


def _flatten(res: dict) -> list | None:
    """Every key, string and number in a result, each on its own, for the number check; None if nested deeper than
    MAX_DEPTH. Walked with a stack, so a hostile body can never blow the recursion limit. The top-level `at` (the
    phone's clock) is left out here and kept only if it is within a day of the Mac's (answer)."""
    out, stack = [], [(k, v, 1) for k, v in res.items() if k != "at"]
    out += [str(k) for k in res]
    while stack:
        _, x, depth = stack.pop()
        if depth > MAX_DEPTH:
            return None
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, bool) or x is None:
            pass
        elif isinstance(x, (int, float)):
            out.append(repr(x))
        elif isinstance(x, dict):
            out += [str(k) for k in x]
            stack += [(k, v, depth + 1) for k, v in x.items()]
        elif isinstance(x, list):
            stack += [("", v, depth + 1) for v in x]
    return out


def _ids(x) -> list:
    """The phone's own opaque ids (find, pick) anywhere in data: they must look like ids, nothing else.
    Only called after _flatten, so the depth is already capped."""
    if isinstance(x, dict):
        return [str(v) for k, v in x.items() if k in ("find", "pick")] + [i for v in x.values() for i in _ids(v)]
    if isinstance(x, list):
        return [i for v in x for i in _ids(v)]
    return []


def _log_result(device: str, order: dict, res: dict):
    data = res.get("data") or {}
    ev = order["kind"] if res["outcome"] == "done" else res["outcome"]
    fields = {"device": device, "order": order["id"], "kind": order["kind"], "outcome": res["outcome"], "ok": res["ok"]}
    if order["kind"] in ACTIONS:                         # who it was for: the watch, never the phone's say-so
        w = next((w for w in _watched() if w["order"] == order["id"]), {})
        fields.update({k: w[k] for k in ("name", "label", "last3") if k in w})
        fields.update({k: w[k] for k in ("len",) if k in w})
        fields.update({k: data[k] for k in ("how", "after_s", "parts") if k in data})
    if order["kind"] == "find":
        fields["who"] = order["args"].get("name", "")
        fields["matches"] = [{k: m.get(k) for k in ("name", "label", "last3", "callable")}
                             for m in data.get("matches", []) if isinstance(m, dict)]
    if res.get("error"):
        fields["error"] = str(res["error"])[:200]
    log(ev, **fields)


def answer(device: str, res: dict) -> tuple[int, str]:
    """The phone's result for one of its delivered orders. (200, '') or (code, why)."""
    why = check_result(res)
    if why:
        return 400, why
    now = time.time()
    at = res.get("at")
    res = {k: v for k, v in res.items() if k != "at"}
    if isinstance(at, (int, float)) and not isinstance(at, bool) and abs(at - now) < 86400:
        res["at"] = at                                  # only a plausible clock reading is kept (never a number)
    with _lock():
        order = load_order(res["id"])
        if not order or order.get("device") != device:
            return 409, "unknown order"
        if order.get("state") == "delivered" and order["expires"] <= now:
            order["state"] = "expired"                  # its life is over: the Mac has told the user it did not happen
            _write(_order_path(order["id"]), _scrub(order))
            log("expired", device=device, order=order["id"], kind=order["kind"], late=True)
        late = (order.get("state") == "expired" and order["kind"] in ACTIONS and order.get("delivered")
                and not _result_path(order["id"]).exists())
        if order.get("state") != "delivered" and not late:
            return 409, f"order is {order.get('state')}"
        # A call or text the phone took but could only report after its life (no signal, phone kept it to resend):
        # what happened is still a fact the user wants to hear, so it is recorded late, never acted on.
        res = {**res, "kind": order["kind"], "received": now, **({"late": True} if late else {})}
        _write(_result_path(order["id"]), res)
        order.update(state="answered", answered=res["received"], **({"late": True} if late else {}))
        _write(_order_path(order["id"]), _scrub(order))
        perms = (res.get("data") or {}).get("perms") if order["kind"] == "ping" and res["ok"] else None
        if isinstance(perms, dict) and isinstance(perms.get("send_sms"), bool):
            devs = devices()                            # how texts go (§11.1): A when granted, else B
            if device in devs:
                devs[device]["send_sms"] = perms["send_sms"]
                _write(DIR / "devices.json", devs)
    _log_result(device, order, res)
    return 200, ""


def wait_result(oid: str, timeout_s: float | None = None, poll_s: float = 0.2) -> dict | None:
    """The connector's wait: the phone's result, or None. Once the order's life is over it is marked expired, so a
    late answer is refused (409). A shorter timeout_s only stops waiting: the order stays live (design §2.4)."""
    order = load_order(oid)
    if not order:
        return None
    end = order["expires"] if timeout_s is None else min(order["expires"], time.time() + timeout_s)
    while True:
        r = _read(_result_path(oid), None)
        if r:
            return r
        if time.time() >= end:
            break
        time.sleep(poll_s)
    if time.time() < order["expires"]:
        return None
    with _lock():
        order = load_order(oid)
        if order and order.get("state") in ("open", "delivered"):
            order["state"] = "expired"
            _write(_order_path(oid), _scrub(order))
            gone = True
        else:
            gone = False
    if gone:
        log("expired", device=order["device"], order=oid, kind=order["kind"], delivered="delivered" in order)
        return None
    return _read(_result_path(oid), None)


def prune(days: int = KEEP_DAYS) -> int:
    scrub_stale()
    cut, n = time.time() - days * 86400, 0
    with _lock():
        for sub in ("orders", "results"):
            for f in (DIR / sub).glob("o_*.json"):
                if f.stat().st_mtime < cut:
                    f.unlink()
                    n += 1
    return n


# ── voice tools: find → token → spoken yes → call (design §4) ──────────────────
# phone_find_contact and phone_call in server.py are thin wrappers round find_contact() and call(). The spoken yes is
# enforced by voice Claude, not by code (design §4 "known risk"); what code enforces is below: tokens are 60 s, one
# use, bound to the phone's pick and the action; using one or a new find cancels every other open token; one
# call/text on its way at a time; 10 call/text orders per 10 min and 100 per 24 h (the user, 3 Nov: was 3 and 10).

TOKEN_LIFE_S = 60
TOKEN_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"         # spoken-safe: no 0/o, 1/l (gitleaks:allow, not a secret)
TOKEN_LEN = 6
OFFLINE_AFTER_S = 40                                        # no /next for this long = the phone is not connected
FIND_WAIT_S = 10
CALL_CONFIRM_S = 3.0                                        # phone_call waits this long to see the phone take it
RATE = ((10, 600, "10 in 10 minutes"), (100, 86400, "100 in a day"))
ACTIONS = ("call", "text")
VOICE_ACTIONS = ("call", "text")
TOKEN_SHAPE = re.compile(r"^[a-z0-9]{4,12}\Z")
BODY_MAX = 300                                              # 2 SMS parts plain, 5 with emoji (design §11.2)
CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029]")    # control characters; a newline is kept
GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
           "¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = set("^{}\\[~]|€\f")                             # two septets each


def body_problem(body) -> str:
    """'' if a text's body may be sent, else the plain reason it may not (design §11.2)."""
    if not isinstance(body, str) or not body.strip():
        return "empty"
    if len(body) > BODY_MAX:
        return "long"
    if CONTROL.search(body):
        return "control"
    return ""


BODY_SAY = {"empty": "There's no text to send. Ask the user what the text should say.",
            "long": f"That text is over {BODY_MAX} characters, too long for the phone helper. Ask the user for a "
                    "shorter one.",
            "control": "That text has hidden control characters in it, so I won't send it. Say it again plainly."}


def sms_parts(body: str) -> int:
    """How many SMS parts the phone's divideMessage makes: GSM-7 160 (153 a part when split), else UCS-2 70 (67)."""
    if all(c in GSM7 or c in GSM7_EXT for c in body):
        n, one, many = sum(2 if c in GSM7_EXT else 1 for c in body), 160, 153
    else:
        n, one, many = len(body.encode("utf-16-le")) // 2, 70, 67
    return 1 if n <= one else -(-n // many)


def send_sms_route(device: str | None) -> bool | None:
    """How the phone sends texts, from its last ping (§11.1): True route A (sends by itself), False route B (the user
    taps a notification and presses Send), None not known yet."""
    v = (devices().get(device or "") or {}).get("send_sms")
    return v if isinstance(v, bool) else None


def _after_text(device: str | None) -> str:
    route = send_sms_route(device)
    if route is True:
        return "hang up and I'll send it in a few seconds."
    if route is False:
        return ("hang up, then tap the 'Tap to text' notification on the phone and press Send; the phone can't send "
                "it by itself yet.")
    return ("hang up and it sends a few seconds later; if the phone shows a 'Tap to text' notification instead, tap "
            "it and press Send.")


def _tokens() -> dict:
    t = _read(DIR / "tokens.json", {})
    return {"tokens": t.get("tokens", []), "queued": t.get("queued", [])} if isinstance(t, dict) else \
        {"tokens": [], "queued": []}


def _save_tokens(t: dict, now: float):
    """Expire stale tokens and drop anything older than a day (the 24 h cap needs no more)."""
    for x in t["tokens"]:
        if x["state"] == "open" and x["expires"] <= now:
            x["state"] = "expired"
    t["tokens"] = [x for x in t["tokens"] if x["expires"] > now - 86400]
    t["queued"] = [q for q in t["queued"] if q > now - 86400]
    _write(DIR / "tokens.json", t)


def _cancel_open(t: dict, keep: str = "") -> int:
    n = 0
    for x in t["tokens"]:
        if x["state"] == "open" and x["sha256"] != keep:
            x["state"] = "cancelled"
            n += 1
    return n


def _rate_hit(t: dict, now: float) -> str:
    """'' if another call/text may go, else the cap that stops it."""
    for most, span, words in RATE:
        if sum(1 for q in t["queued"] if q > now - span) >= most:
            return words
    return ""


def _pending(device: str, now: float) -> dict | None:
    """A call or text already on its way to this phone (open or delivered, life not over)."""
    for f in (DIR / "orders").glob("o_*.json"):
        o = _read(f, None)
        if (isinstance(o, dict) and o.get("device") == device and o.get("kind") in ACTIONS
                and o.get("state") in ("open", "delivered") and o.get("expires", 0) > now):
            return o
    return None


def phone_online(device: str | None = None) -> tuple[str | None, str]:
    """(device, '') when the phone is connected, else (device or None, a plain spoken reason)."""
    device = device or default_device()
    if not device:
        return None, "No phone is paired with the Mac yet, so I can't call from it."
    d = devices().get(device) or {}
    if not d or d.get("revoked"):
        return device, "That phone isn't paired with the Mac any more, so I can't call from it."
    seen = d.get("last_seen", 0)
    if time.time() - seen > OFFLINE_AFTER_S:
        return device, (f"The phone isn't connected right now, last seen {_ago(seen)}. "
                        "It may be off, out of signal or not running the phone helper.")
    return device, ""


def status() -> str:
    """phone_status: is the phone helper connected, and when was it last seen (design §2.4). Read only."""
    device = default_device()
    if not device:
        return "No phone is paired with the Mac yet."
    d = devices().get(device) or {}
    seen = d.get("last_seen", 0)
    how = f" on {'mobile data' if d.get('net') == 'cellular' else d['net']}" if d.get("net") in ("wifi", "cellular") else ""
    app = f" App version {d['app_version']}." if d.get("app_version") else ""
    if time.time() - seen > OFFLINE_AFTER_S:
        return (f"The phone isn't connected right now, last seen {_ago(seen)}{how}. It may be off, out of signal "
                f"or not running the phone helper; it reconnects by itself when it can.{app}")
    return f"The phone is connected{how}, last seen {_ago(seen)}.{app}"


def _digits(last3: str) -> str:
    return "-".join(str(last3))


def _where(m: dict) -> str:
    where = "an overseas number" if m.get("overseas") else f"{(m.get('label') or 'number').lower()}"
    return f"{where} ending {_digits(m.get('last3', ''))}"


def _say(m: dict) -> str:
    return f"{m.get('name', '?')}, {_where(m)}"


def find_contact(name: str, action: str = "call", body: str = "") -> str:
    """phone_find_contact: ask the phone, mint a token per callable match, say what to read back. For a text the
    token is also bound to sha256(body); the body itself never goes to the phone at find (design §11.2)."""
    name = (name or "").strip()
    action = (action or "call").strip().lower()
    if action not in ACTIONS:
        return f"I can only look someone up to call or text, not '{action}'."
    if action not in VOICE_ACTIONS:
        return "Texts aren't built into the phone helper yet, only calls. The user can send this one himself."
    if action == "text":
        body = body.strip() if isinstance(body, str) else body    # spaces round the edges are never part of a text
        bad = body_problem(body)
        if bad:
            log("refused", kind="find", action="text", why=f"body {bad}", len=len(body or ""))
            return BODY_SAY[bad]
    scrub_stale()
    if not name or NUMBER_LIKE.search(name) or len(name) > 60:
        log("refused", kind="find", why="not a contact name")
        return "I can only call saved contacts by name, never a spoken number. Ask the user who to call."
    device, why = phone_online()
    if why:
        log("offline", device=device or "", kind="find", who=name, why=why)
        return why
    now = time.time()
    with _lock():
        t = _tokens()
        n = _cancel_open(t)                                 # a new find ends every older token (design §4)
        capped = _rate_hit(t, now)
        _save_tokens(t, now)
    if n:
        log("token_cancelled", device=device, n=n, why="new find")
    if capped:
        log("refused", device=device, kind="find", who=name, why=f"rate cap {capped}")
        return (f"I've already placed {capped} from the phone, which is the most I'm allowed. "
                "the user can dial this one himself, or ask again a bit later.")
    try:
        order = send("find", {"name": name, "action": action}, device=device)
    except (LookupError, ValueError) as e:
        log("refused", device=device, kind="find", who=name, why=str(e))
        return "I couldn't ask the phone just now."
    res = wait_result(order["id"], FIND_WAIT_S)
    if res is None:
        log("refused", device=device, order=order["id"], kind="find", who=name, why="no answer")
        return "The phone didn't answer in time. It may have just lost signal; try again in a moment."
    if not res.get("ok"):
        err = str(res.get("error") or res.get("outcome") or "it couldn't look")[:120]
        return f"The phone couldn't look that up: {err}."
    data = res.get("data") or {}
    matches = [m for m in data.get("matches", []) if isinstance(m, dict)][:3]
    if not matches:
        return f"There's no saved contact on the phone called {name}. Ask the user for another name."
    find_id, lines, made = str(data.get("find", "")), [], []
    with _lock():
        t, now = _tokens(), time.time()
        _cancel_open(t)                                     # nothing older survives this find
        for i, m in enumerate(matches, 1):
            if not m.get("callable") or not OPAQUE_ID.match(str(m.get("pick", ""))) or not OPAQUE_ID.match(find_id):
                why_not = str(m.get("why_not") or "it's a number I'm not allowed to ring")
                lines.append(f"{i}. {_say(m)}: can't {action}, {why_not}.")
                continue
            tok = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LEN))
            t["tokens"].append({"sha256": _sha(tok), "device": device, "find": find_id, "pick": str(m["pick"]),
                                "action": action, "name": str(m.get("name", "")), "label": str(m.get("label", "")),
                                "last3": str(m.get("last3", "")), "overseas": bool(m.get("overseas")),
                                **({"body_sha256": _sha(body)} if action == "text" else {}),
                                "issued": now, "expires": now + TOKEN_LIFE_S, "state": "open"})
            made.append((tok, m))
            lines.append(f"{i}. {_say(m)}: token {tok}")
        _save_tokens(t, now)
    for tok, m in made:
        log("token", device=device, order=order["id"], action=action, name=m.get("name", ""),
            last3=m.get("last3", ""), token=tok[:2], **({"len": len(body)} if action == "text" else {}))
    verb = "call" if action == "call" else "text"
    if not made:
        return ("I found them, but not a number I'm allowed to " + verb + ":\n" + "\n".join(lines)
                + "\nTell the user plainly; never try another way.")
    if action == "call":
        after, ask, use = "Then tell them: hang up and I'll ring in a few seconds.", "Shall I call?", "phone_call"
        what = ""
    else:
        after, ask = f"Then tell them: {_after_text(device)}", "Shall I send it?"
        use = "phone_text with the same text, word for word,"
        what = f": '{body}'"
    if len(made) == 1 and len(lines) == 1:
        tok, m = made[0]
        return (f"Read back: {_say(m)}{what}. Ask '{ask}' and call {use} with token {tok} ONLY after "
                f"the user says yes. {after} The token works once, within 60 seconds; after that look them up again.")
    return (f"I found {len(lines)}:\n" + "\n".join(lines) + f"\nAsk the user which one. Read back the one he picks "
            f"(name, the kind of number, and its last three digits{', then the text' if what else ''}) and ask "
            f"'{ask}'. Call {use} with that one's token ONLY after he says yes; the others are cancelled the moment "
            f"one is used. {after} Tokens work once, within 60 seconds; after that look them up again."
            + (f"\nThe text{what}" if what else ""))


def _use_token(token: str, action: str, body: str | None = None) -> tuple[dict | None, str]:
    """Spend one token and queue its order: (order, '') or (None, a plain spoken reason). All under the lock, so at
    most one call or text can ever be on its way."""
    token = (token or "").strip().lower()
    now = time.time()
    refused = None
    with _lock():
        t = _tokens()
        x = next((x for x in t["tokens"] if TOKEN_SHAPE.match(token) and hmac.compare_digest(x["sha256"], _sha(token))),
                 None)
        if x is None:
            refused = ("wrong", "That token doesn't match a lookup. Look the person up again with phone_find_contact.")
        elif x["state"] == "used":
            refused = ("used", "That token has already been used. Look the person up again if the user wants another.")
        elif x["state"] == "cancelled":
            refused = ("cancelled", "That token was cancelled by a newer lookup or call. Look the person up again.")
        elif x["state"] == "expired" or x["expires"] <= now:
            refused = ("expired", "That lookup is over a minute old, so it has run out. Look the person up again "
                                  "and read it back to the user.")
        elif x["action"] != action:
            refused = ("action", f"That token was for a {x['action']}, not a {action}. Look them up again.")
        elif action == "text" and not hmac.compare_digest(x.get("body_sha256", ""), _sha(body or "")):
            refused = ("body_changed", "The text changed since I read it back, so I'll read it again: look them up "
                                       "again with the text exactly as the user wants it.")
        else:
            device, why = phone_online(x["device"])
            if why:
                refused = ("offline", why)
            elif _pending(x["device"], now):
                refused = ("pending", "A call or text is already on its way to the phone. Wait for it first.")
            elif _rate_hit(t, now):
                words = _rate_hit(t, now)
                _cancel_open(t)
                refused = ("rate", f"I've already placed {words} from the phone, which is the most I'm allowed. "
                                   "the user can dial this one himself.")
        if refused:
            _save_tokens(t, now)
        else:
            x["state"] = "used"
            x["used"] = now
            n = _cancel_open(t, keep=x["sha256"])
            t["queued"].append(now)
            extra = {"body": body} if action == "text" else {}
            order = _make_order(action, {"find": x["find"], "pick": x["pick"], **extra}, x["device"], None)
            _save_tokens(t, now)                            # spent and others cancelled BEFORE the order exists
            _write(_order_path(order["id"]), order)
            _watch(order, x, now, len(body) if action == "text" else None)
    if refused:
        kind, why = refused
        log("token_refused", device=(x or {}).get("device", ""), action=action, token=token[:2], why=kind,
            **({"name": x["name"], "last3": x["last3"]} if x else {}))
        return None, why
    words = {"len": len(body), "parts": sms_parts(body)} if action == "text" else {}    # never the words (§11.3)
    log(action, device=x["device"], order=order["id"], name=x["name"], label=x["label"], last3=x["last3"],
        token=token[:2], cancelled=n, **words)
    return order, ""


def _taken(order: dict) -> bool:
    """Wait up to CALL_CONFIRM_S for the phone to take the order; True if it has."""
    end = time.time() + CALL_CONFIRM_S
    while time.time() < end and (load_order(order["id"]) or {}).get("state") == "open":
        time.sleep(POLL_S)
    return (load_order(order["id"]) or {}).get("state") != "open"


def call(token: str) -> str:
    """phone_call: queue the call the token was made for, then say what the user does next."""
    scrub_stale()
    order, why = _use_token(token, "call")
    if not order:
        return why
    got = _taken(order)
    return ("Done: the phone has it. Tell the user to hang up now; it rings a few seconds after this call ends. "
            if got else "Queued: the phone hasn't picked it up yet but should within a few seconds. Tell the user to "
            "hang up now; it rings a few seconds after this call ends. ") + \
        "If this call is still going in a minute, the phone won't ring and I'll say so next call."


def text(token: str, body: str) -> str:
    """phone_text: queue the text the token was made for, only if the body is the one read back (design §11.2), then
    say what the user does next: route A it sends itself, route B he taps the notification (from the last ping)."""
    scrub_stale()
    body = body.strip() if isinstance(body, str) else body
    bad = body_problem(body)
    if bad:
        log("token_refused", action="text", token=(token or "").strip().lower()[:2], why=f"body_{bad}",
            len=len(body) if isinstance(body, str) else 0)
        return BODY_SAY[bad]
    order, why = _use_token(token, "text", body)
    if not order:
        return why
    got = _taken(order)
    route = send_sms_route(order["device"])
    head = "Done: the phone has it." if got else "Queued: the phone hasn't picked it up yet but should within a few seconds."
    if route is False:
        what = ("Tell the user to hang up now, then tap the 'Tap to text' notification on the phone and press Send; "
                "the phone can't send texts by itself yet.")
    elif route is True:
        what = "Tell the user to hang up now; it sends a few seconds after this call ends."
    else:
        what = ("Tell the user to hang up now; it sends a few seconds after this call ends, or, if the phone shows a "
                "'Tap to text' notification, he taps it and presses Send.")
    return f"{head} {what} If this call is still going in a minute, the phone won't send it and I'll say so next call."


# ── Outcomes to the user (design §6.1, §8; step outcomes-to-voice) ──────────────
# phone_call returns before the phone rings (the user has to hang up first), so every queued call or text is watched
# in pending.json. call_start's rundown then says, once, on their next Claude call, how each went: sent (with the time,
# and delivered when the network said so), rang out, Tap-to-text posted, or did not go through: cancelled (the Claude
# call never ended), refused, failed, or never answered (corrected later if the phone reports in once back online).

TELL_KEEP_S = 3 * 86400                                      # call_start looks back at most 3 days, so do we


def _watched() -> list:
    w = _read(DIR / "pending.json", {})
    return [x for x in w.get("watch", []) if isinstance(x, dict) and "order" in x] if isinstance(w, dict) else []


def _save_watched(watch: list, now: float):
    _write(DIR / "pending.json", {"watch": [x for x in watch if x.get("queued", 0) > now - TELL_KEEP_S]})


def _watch(order: dict, tok: dict, now: float, body_len: int | None = None):
    """Called under the lock as the order is made: remember who it is for (name, kind of number, last 3 only; for a
    text its length, never the words)."""
    watch = _watched()
    watch.append({"order": order["id"], "action": order["kind"], "device": order["device"], "name": tok["name"],
                  "label": tok["label"], "last3": tok["last3"], "overseas": tok.get("overseas", False),
                  **({"len": body_len} if body_len is not None else {}), "queued": now, "told": None})
    _save_watched(watch, now)


def _clock(t: float, now: float) -> str:
    """'4:05 pm', or 'yesterday at 4:05 pm' / 'on Monday at 4:05 pm' when not today (the Mac's local time)."""
    at, today = time.localtime(t), time.localtime(now)
    hm = time.strftime("%I:%M %p", at).lstrip("0").lower()
    if at.tm_yday == today.tm_yday and at.tm_year == today.tm_year:
        return hm
    if now - t < 2 * 86400 and time.localtime(now - 86400).tm_yday == at.tm_yday:
        return f"yesterday at {hm}"
    return f"on {time.strftime('%A', at)} at {hm}"


def _done_at(res: dict) -> float:
    """When it happened: the phone's clock at the order's arrival (`at`) plus data.sent_after_s (app 0.3.0+), else
    when the Mac heard (the phone reports straight after, unless it was offline)."""
    at, after = res.get("at"), (res.get("data") or {}).get("sent_after_s")
    if (isinstance(at, (int, float)) and isinstance(after, (int, float)) and not isinstance(after, bool)
            and 0 <= after < 3600):
        return float(at) + after
    return float(res.get("received") or time.time())


def _success_line(w: dict, res: dict, now: float) -> str:
    """One spoken sentence for a call or text that went: sent / rang out / Tap-to-text posted, with the time."""
    who = f"{w['name']} ({_where(w)})"
    data = res.get("data") or {}
    when = _clock(_done_at(res), now)
    test = " (a dry run: nothing really went)" if data.get("how") == "dry_run" else ""
    if w["action"] == "call":
        return (f"The call to {who} went through at {when}{test}: the phone rang out. It can't tell whether they "
                f"answered.")
    if res.get("outcome") == "done":                     # route B: Messages opened pre-filled (§11.4)
        return (f"The text to {who} was put on the phone as a 'Tap to text' notification at {when}; it only went "
                f"if he pressed Send in Messages.")
    delivered = {True: ", and the network says it was delivered", False: ", but the network says it was not delivered"}
    return f"Your text to {who} sent at {when}{test}{delivered.get(data.get('delivered'), '')}."


def _outcome_line(w: dict, res: dict | None) -> str:
    """One spoken sentence for a call or text that did not go through."""
    who = f"{w['name']} ({_where(w)})"
    verb, did = ("ring", "rang") if w["action"] == "call" else ("text", "texted")
    why = str((res or {}).get("error") or "").strip().rstrip(".")
    out = (res or {}).get("outcome")
    if res is None:
        where = "call log" if w["action"] == "call" else "Messages"
        return (f"I'm not sure the phone {did} {who}: it never told the Mac how it went, so it may have lost signal. "
                f"Check the phone's {where} or {verb} them yourself.")
    if out == "cancelled":
        return (f"I didn't {verb} {who}: the voice call was still going a minute after the user said yes, so the phone "
                f"gave up. Offer to try again, and remind them to hang up straight after.")
    if out == "refused":
        return (f"The phone refused to {verb} {who}" + (f": {why}" if why else "")
                + (". Nothing was dialled." if w["action"] == "call" else ". Nothing was sent."))
    if out == "failed" and w["action"] == "call" and (res.get("data") or {}).get("how") == "notification":
        return (f"The phone couldn't {verb} {who} by itself, so it put a 'Tap to call' notification on the phone "
                f"instead. If he didn't tap it, it didn't ring.")
    if out == "unsupported":
        return f"I couldn't {verb} {who}: the phone helper app on the phone is too old and needs updating."
    return f"The phone tried to {verb} {who} but it didn't go through" + (f": {why}" if why else "") + "."


def outcomes(mark: bool = True, now: float | None = None) -> list[str]:
    """How each watched call or text went, as spoken lines, failures first; marked told (so each is said once)
    unless mark is False. One still on its way is left for later. One said as "not sure" is said again, once, if the
    phone reports it after all (it was offline and resent its report on reconnecting)."""
    now = now or time.time()
    bad, good = [], []
    scrub_stale()                                       # the real clock: a body never outlives its order
    with _lock():
        watch = [w for w in _watched() if w.get("queued", 0) > now - TELL_KEEP_S]   # older is past saying
        for w in watch:
            res = _read(_result_path(w["order"]), None)
            if w.get("told"):
                if not (w.get("unsure") and res is not None):
                    continue
                line = _line(w, res, now)                       # the phone reported in after all
                (good if _went(res) else bad).append("Update on the one I wasn't sure of: " + line)
                if mark:
                    w["unsure"] = False
                continue
            order = load_order(w["order"]) or {}
            if res is None and order.get("state") in ("open", "delivered") and order.get("expires", 0) > now:
                continue                                    # still on its way: say nothing yet
            (good if res is not None and _went(res) else bad).append(_line(w, res, now))
            if mark:
                w["told"] = now
                w["unsure"] = res is None
        if mark:
            _save_watched(watch, now)
    return bad + good


def _went(res: dict) -> bool:
    return res.get("outcome") in ("dialled", "sent") or (
        res.get("outcome") == "done" and (res.get("data") or {}).get("how") == "messages_app")


def _line(w: dict, res: dict | None, now: float) -> str:
    return _success_line(w, res, now) if res is not None and _went(res) else _outcome_line(w, res)


def call_start_lines(mark: bool = True) -> list[str]:
    """For call_start's rundown: one line saying how every call or text went since the last rundown, said once."""
    said = outcomes(mark)
    if not said:
        return []
    return ["PHONE (tell the user early, once): " + " ".join(said)]


# ── Inbox: incoming texts and missed calls (the user, 4 Nov 2026 ─────────────────────
# The phone (0.2.3+) posts each new incoming SMS and missed call to /phone/api/inbox: who (a saved contact's name, a
# sender name like "Telstra", or "" with only the last 3 digits), when, and for a text its words. call_start says
# missed calls and who texted, once each; phone_inbox reads the texts out and marks them heard. The words stay in
# inbox.json on the Mac (7 days, dir 700, files 600) and are never logged; a one-time code is held back unless
# the user asks for it.

INBOX_READS = ("sms,calls", "sms", "calls", "none")   # X-Phone-Reads values
INBOX_KINDS = ("sms", "missed")
INBOX_MAX_POST = 50
INBOX_MAX_KEEP = 300
INBOX_KEEP_S = 7 * 86400
INBOX_BODY_MAX = 2000
INBOX_ITEM_KEYS = {"kind", "at", "who", "known", "last3", "body"}
LAST3 = re.compile(r"^\d{0,3}\Z")
CODE_WORDS = re.compile(r"\b(code|verif\w*|otp|one[- ]time|passcode|pin|security|authenticat\w*|log ?in|sign ?in)\b", re.I)
CODE_DIGITS = re.compile(r"(?<!\d)\d{4,8}(?!\d)")


def looks_like_code(body: str) -> bool:
    """A one-time login or verification code (bank, MyGov, Google): held back from voice unless the user asks."""
    return bool(CODE_WORDS.search(body) and CODE_DIGITS.search(body))


def check_inbox(body) -> str:
    """'' if the phone's inbox post has the expected shape, else why not. Who and last3 never look like a number."""
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        return "items must be a list"
    if len(body["items"]) > INBOX_MAX_POST:
        return f"at most {INBOX_MAX_POST} items"
    now = time.time()
    for it in body["items"]:
        if not isinstance(it, dict) or set(it) - INBOX_ITEM_KEYS:
            return "unknown item field"
        if it.get("kind") not in INBOX_KINDS:
            return "unknown kind"
        at = it.get("at")
        if not isinstance(at, (int, float)) or isinstance(at, bool) or not (now - INBOX_KEEP_S < at < now + 86400):
            return "bad time"
        who = it.get("who", "")
        if not isinstance(who, str) or len(who) > 60 or NUMBER_LIKE.search(who) or CONTROL.search(who):
            return "who must be a name, never a number"
        if not isinstance(it.get("known", False), bool) or not LAST3.match(str(it.get("last3", ""))):
            return "bad known or last3"
        b = it.get("body")
        if it["kind"] == "sms" and not isinstance(b, str):
            return "a text needs its words"
        if it["kind"] == "missed" and b is not None:
            return "a missed call has no words"
    return ""


def _inbox() -> list:
    x = _read(DIR / "inbox.json", {})
    return [i for i in x.get("items", []) if isinstance(i, dict)] if isinstance(x, dict) else []


def _save_inbox(items: list, now: float):
    keep = [i for i in items if i.get("at", 0) > now - INBOX_KEEP_S]
    _write(DIR / "inbox.json", {"items": sorted(keep, key=lambda i: i["at"])[-INBOX_MAX_KEEP:]})


def add_inbox(device: str, body: dict, now: float | None = None) -> int:
    """Keep the phone's new items (a resend of one already kept is dropped). Returns how many were new."""
    now = now or time.time()
    new = 0
    with _lock():
        items = _inbox()
        seen = {i["key"] for i in items if "key" in i}
        for it in body["items"]:
            words = CONTROL.sub(" ", it.get("body") or "")[:INBOX_BODY_MAX].strip()
            key = _sha(json.dumps([it["kind"], round(float(it["at"]), 3), it.get("who", ""), it.get("last3", ""),
                                   words]))[:16]
            if key in seen:
                continue
            seen.add(key)
            items.append({"key": key, "kind": it["kind"], "at": float(it["at"]), "who": it.get("who", "").strip(),
                          "known": bool(it.get("known")), "last3": str(it.get("last3", "")), "device": device,
                          **({"body": words, "code": looks_like_code(words)} if it["kind"] == "sms" else {}),
                          "received": now, "flagged": None, "heard": None})
            new += 1
        _save_inbox(items, now)
    if new:                                              # who and when only: never the words
        log("inbox", device=device, new=new,
            sms=sum(1 for i in body["items"] if i["kind"] == "sms"),
            missed=sum(1 for i in body["items"] if i["kind"] == "missed"))
    return new


def _who(i: dict) -> str:
    """'Lena', 'Telstra (not a saved contact)', 'an unknown number ending 123', 'a private number'."""
    if i.get("who") and i.get("known"):
        return i["who"]
    if i.get("who"):
        return f"{i['who']} (not a saved contact)"
    if i.get("last3"):
        return f"an unknown number ending {_digits(i['last3'])}"
    return "a private number"


def _inbox_line(i: dict, now: float, show_codes: bool = False) -> str:
    when = _clock(i["at"], now)
    if i["kind"] == "missed":
        return f"Missed call from {_who(i)} at {when}."
    if i.get("code") and not show_codes:
        return (f"Text from {_who(i)} at {when}: it looks like a one-time security code, so I'm not reading it out. "
                f"Say if you want it.")
    return f"Text from {_who(i)} at {when}: \"{i.get('body', '')}\""


def _reads_note() -> str:
    """What the phone can't read yet, as one plain step for the user ("" when it can read both, or no phone)."""
    dev = default_device()
    d = devices().get(dev or "", {})
    if not dev or not d.get("app_version"):
        return ""
    reads = d.get("reads")
    if reads is None:
        return " (The phone helper app is older than 0.2.3, so it doesn't send texts or missed calls yet: install the update.)"
    gaps = [w for k, w in (("sms", "texts"), ("calls", "missed calls")) if k not in reads.split(",")]
    if not gaps:
        return ""
    return (f" (The phone isn't allowed to read {' or '.join(gaps)} yet: the user opens Phone helper and taps "
            f"\"Allow texts and calls\".)")


def inbox(recent: bool = False, show_codes: bool = False, mark: bool = True, now: float | None = None) -> str:
    """phone_inbox: every text and missed call the user has not heard yet, oldest first, then marked heard. recent:
    also the ones already heard in the last day ("read them again")."""
    now = now or time.time()
    with _lock():
        items = _inbox()
        want = [i for i in items if not i.get("heard") or (recent and i["heard"] > now - 86400)]
        if mark:
            for i in want:
                i["heard"] = i.get("heard") or now
                i["flagged"] = i.get("flagged") or now
            _save_inbox(items, now)
    dev, offline = phone_online()
    stale = ("" if not offline or not dev else (
        " (The phone isn't connected to the Mac right now, so newer ones may not have come through.)")) + _reads_note()
    if not want:
        return ("No new texts or missed calls." if not recent else "No texts or missed calls in the last day.") + stale
    texts = sum(1 for i in want if i["kind"] == "sms")
    missed = len(want) - texts
    head = " and ".join(x for x in (_n(texts, "text"), _n(missed, "missed call")) if x)
    lines = [f"{n}. {_inbox_line(i, now, show_codes)}" for n, i in enumerate(want, 1)]
    return (f"{head}, oldest first. Read them to the user ONE AT A TIME and pause after each. To reply to a saved "
            f"contact, use phone_find_contact(name, action=\"text\", body=their words) and the usual read-back and yes. "
            f"Unknown numbers can't be texted or rung (saved contacts only).{stale}\n" + "\n".join(lines))


def _n(k: int, word: str) -> str:
    return "" if not k else f"{'one' if k == 1 else k} {'new ' if word == 'text' else ''}{word}{'' if k == 1 else 's'}"


def inbox_call_start_lines(mark: bool = True, now: float | None = None) -> list[str]:
    """For call_start, near the top: missed calls said in full, and who has texted (the words wait for phone_inbox).
    Each said once; a text stays unheard until phone_inbox reads it."""
    now = now or time.time()
    with _lock():
        items = _inbox()
        fresh = [i for i in items if not i.get("flagged") and not i.get("heard")]
        if mark and fresh:
            for i in fresh:
                i["flagged"] = now
                if i["kind"] == "missed":
                    i["heard"] = now             # said in full here, so phone_inbox does not say it again
            _save_inbox(items, now)
    nudge = _reads_nudge(mark)
    if not fresh:
        return nudge
    missed = [i for i in fresh if i["kind"] == "missed"]
    texts = [i for i in fresh if i["kind"] == "sms"]
    said = []
    if missed:
        said.append(" ".join(f"{_who(i)[0].upper()}{_who(i)[1:]} tried to call at {_clock(i['at'], now)}."
                             for i in missed))
    if texts:
        senders = list(dict.fromkeys(_who(i) for i in texts))
        said.append(f"{_n(len(texts), 'text')[0].upper()}{_n(len(texts), 'text')[1:]} from {', '.join(senders)}: "
                    f"offer to read {'it' if len(texts) == 1 else 'them'} (phone_inbox).")
    return ["PHONE INBOX (tell the user near the top, once): " + " ".join(said)] + nudge


def _reads_nudge(mark: bool) -> list[str]:
    """Once per change: the 0.2.3+ app is on the phone but not allowed to read texts or calls yet (an update in place
    never grants a new permission, so the user has to tap it)."""
    dev = default_device()
    if not dev:
        return []
    with _lock():
        devs = devices()
        d = devs.get(dev, {})
        reads = d.get("reads")
        if reads is None or reads == "sms,calls" or d.get("reads_told") == reads:
            return []
        if mark:
            d["reads_told"] = reads
            _write(DIR / "devices.json", devs)
    return ["PHONE INBOX (tell the user once): the phone helper can't read their texts or missed calls yet. One step: open "
            "Phone helper on the phone and tap \"Allow texts and calls\"."]


# ── HTTP (served by approvals.py under /phone) ─────────────────────────────────

def _j(code: int, obj) -> tuple[int, bytes]:
    return code, json.dumps(obj).encode()


def handle(method: str, raw_path: str, headers, body_reader, ip: str = "", want_login: str = "",
           test_identity: str = "", gone=lambda: False) -> tuple[int, bytes]:
    """One /phone request → (status, JSON body; empty for 204). body_reader(n) reads n bytes and is only called
    once identity, key and size have passed. test_identity (the emulator harness daemon only, design §9): a
    request with no Tailscale header counts as that login, and only that login is accepted."""
    u = urllib.parse.urlsplit(raw_path)
    who = headers.get("Tailscale-User-Login", "") or test_identity
    want = test_identity or want_login
    if not want or who != want:                                         # 1. tailnet identity, fails closed
        _refuse(ip, "", f"no tailnet identity ({who or 'none'})" if not who else f"wrong tailnet user ({who})")
        return _j(403, {"error": "tailnet only"})
    routes = {"/phone/api/pair": "POST", "/phone/api/next": "GET", "/phone/api/result": "POST",
              "/phone/api/inbox": "POST"}
    if u.path not in routes:
        return _j(404, {"error": "not found"})
    if method != routes[u.path]:
        return _j(405, {"error": f"{routes[u.path]} only"})
    device = headers.get("X-Phone-Device", "")
    if u.path != "/phone/api/pair":                                     # 2. device key
        auth = headers.get("Authorization", "")
        key = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not DEVICE_ID.match(device) or not authenticate(device, key):
            _refuse(ip, device if DEVICE_ID.match(device) else "", "missing device key" if not key else "wrong device key")
            return _j(401, {"error": "device not paired"})
    if u.path == "/phone/api/next":
        q = urllib.parse.parse_qs(u.query)
        try:
            wait_s = float(q.get("wait", [MAX_WAIT_S])[0])
        except ValueError:
            wait_s = MAX_WAIT_S
        ver, net = headers.get("X-App-Version", ""), headers.get("X-Phone-Net", "")
        touch(device, "" if NUMBER_LIKE.search(ver) else ver, net if net in ("wifi", "cellular", "other") else "",
              headers.get("X-Phone-Reads", ""))
        o = wait_next(device, wait_s, gone)
        return (200, json.dumps(o).encode()) if o else (204, b"")
    try:                                                                # 3. size, then shape
        n = int(headers.get("Content-Length") or 0)
    except ValueError:
        return _j(400, {"error": "bad length"})
    if n < 0:
        return _j(400, {"error": "bad length"})
    if n > MAX_BODY:
        log("refused_body", device=device, path=u.path, length=n)
        return _j(413, {"error": "too big"})
    try:
        body = json.loads(body_reader(n) or b"null")
    except Exception:
        return _j(400, {"error": "not JSON"})
    if u.path == "/phone/api/pair":
        if not isinstance(body, dict) or not isinstance(body.get("code"), str):
            return _j(400, {"error": "code needed"})
        if any(NUMBER_LIKE.search(str(body.get(k, ""))) for k in ("name", "app_version")):
            return _j(400, {"error": "name and version must not look like a phone number"})
        got = pair(body["code"].strip(), str(body.get("name", ""))[:40], str(body.get("app_version", "")))
        if not got:
            log("pair_refused", name=str(body.get("name", ""))[:40])
            N.auth_failed(ip, "phone pairing with a wrong or used code")
            return _j(403, {"error": "wrong or used code"})
        return _j(200, {"device": got[0], "key": got[1]})
    if u.path == "/phone/api/inbox":
        why = check_inbox(body)
        if why:
            log("bad_inbox", device=device, why=why)
            return _j(400, {"error": why})
        return _j(200, {"ok": True, "new": add_inbox(device, body)})
    code, why = answer(device, body)
    if code == 400:
        log("bad_result", device=device, why=why)
    return _j(code, {"ok": True} if code == 200 else {"error": why})


def _refuse(ip: str, device: str, why: str):
    log("auth_refused", device=device, ip=ip, why=why)
    N.auth_failed(ip or "?", f"phone channel: {why}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def _ago(t: float) -> str:
    if not t:
        return "never"
    s = int(time.time() - t)
    return f"{s} s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


def _span(s: float) -> str:
    return f"{int(s // 60)} min" if s < 7200 else f"{s / 3600:g} h"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair", help="print a one-time pairing code")
    p.add_argument("--name", default="my-phone"); p.add_argument("--test", action="store_true")
    p.add_argument("--hours", type=float, default=None, help="life of the code (default 15 min, at most 12)")
    sub.add_parser("devices")
    sub.add_parser("status", help="what phone_status tells voice Claude")
    r = sub.add_parser("revoke"); r.add_argument("device")
    s = sub.add_parser("send", help="queue an order (testing)")
    s.add_argument("kind"); s.add_argument("--name", default=None); s.add_argument("--action", default=None)
    s.add_argument("--arg", action="append", default=[], help="k=v, repeatable")
    s.add_argument("--device", default=None); s.add_argument("--wait", action="store_true")
    s.add_argument("--timeout", type=float, default=None); s.add_argument("--life", type=float, default=None)
    lg = sub.add_parser("log"); lg.add_argument("-n", type=int, default=20)
    oc = sub.add_parser("outcomes", help="calls/texts that did not go through, as call_start says them")
    oc.add_argument("--peek", action="store_true", help="do not mark them told")
    sub.add_parser("prune")
    ns = ap.parse_args(argv)
    if ns.cmd == "pair":
        life = min(ns.hours * 3600, CODE_MAX_LIFE_S) if ns.hours else CODE_LIFE_S
        code = pair_code(ns.name, test=ns.test, life_s=life)
        print(f"{code}  (for {ns.name}; works once, for {_span(life)})")
    elif ns.cmd == "devices":
        devs = devices()
        if not devs:
            print("no paired phone")
        for d, v in devs.items():
            print(f"{d}  {v['name']:16} v{v.get('app_version') or '?':8} {v.get('net') or '-':8} "
                  f"seen {_ago(v.get('last_seen', 0))}{'  REVOKED' if v.get('revoked') else ''}")
    elif ns.cmd == "status":
        print(status())
    elif ns.cmd == "revoke":
        if not revoke(ns.device):
            print("no such device", file=sys.stderr)
            return 1
        print(f"revoked {ns.device}")
    elif ns.cmd == "send":
        args = dict(a.split("=", 1) for a in ns.arg if "=" in a)
        if ns.name is not None:
            args["name"] = ns.name
        if ns.action is not None:
            args["action"] = ns.action
        try:
            order = send(ns.kind, args, device=ns.device, life_s=ns.life)
        except (LookupError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        if not ns.wait:
            print(order["id"])
            return 0
        res = wait_result(order["id"], ns.timeout)
        if res is None:
            print(f"{order['id']}: no answer from the phone", file=sys.stderr)
            return 2
        print(json.dumps(res, indent=1))
        return 0 if res.get("ok") else 1
    elif ns.cmd == "log":
        for line in read_log(ns.n):
            print(json.dumps(line))
    elif ns.cmd == "outcomes":
        for line in outcomes(mark=not ns.peek):
            print(line)
    elif ns.cmd == "prune":
        print(f"pruned {prune()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
