"""The phone's side door into your Mac: a small web server the phone helper app keeps one connection open to.

It only serves /phone/api/* (phone.handle does the checks: your tailnet identity, then the paired device key, then
size and shape). It listens on 127.0.0.1 only; `tailscale serve` puts it on your tailnet over HTTPS and adds the
Tailscale-User-Login header that proves the request came from you. Never expose it with Funnel or a public tunnel.

    python3 phone_server.py serve                  run it (launchd/systemd keeps it running; see the README)
    python3 phone_server.py serve --port 8769 --phone-test-identity emulator@test
                                                   the emulator test instance (needs PHONE_HELPER_DIR set)

Config: ~/.config/phone-helper/config.json, e.g. {"tailnet_login": "you@example.com", "port": 8770}.
tailnet_login is the login Tailscale shows for you (`tailscale status --json` → User → LoginName). Empty = every
request refused (fails closed).
"""
import argparse
import json
import os
import select
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("PHONE_HELPER_CONFIG_DIR") or Path.home() / ".config" / "phone-helper")
STORE = ROOT
CONFIG_FILE = ROOT / "config.json"
DEFAULTS = {"tailnet_login": "", "port": 8770}


def config() -> dict:
    c = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        c.update(json.loads(CONFIG_FILE.read_text()))
    return c


class Handler(BaseHTTPRequestHandler):
    server_version = "phone-helper"
    sys_version = ""

    def log_message(self, fmt, *args):          # phone.py keeps its own log; never print request lines (no numbers)
        pass

    def _phone(self):
        import phone
        path = urllib.parse.urlsplit(self.path).path
        if not (path == "/phone" or path.startswith("/phone/")):
            self._send(404, b'{"error":"not found"}')
            return
        self.connection.settimeout(30)          # a client that sends less than its Content-Length cannot hold a thread
        code, body = phone.handle(self.command, self.path, self.headers, self.rfile.read, ip=self.client_address[0],
                                  want_login=config()["tailnet_login"],
                                  test_identity=getattr(self.server, "phone_test_identity", ""), gone=self._gone)
        self._send(code, body)

    def _gone(self) -> bool:
        """True once the phone closed this connection, so a long-poll held for a killed app never takes an order."""
        try:
            if not select.select([self.connection], [], [], 0)[0]:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _send(self, code: int, body: bytes):
        self.send_response(code)
        if code != 204:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if code != 204:
            self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = _phone


def serve(port: int | None = None, block: bool = True, phone_test_identity: str = "") -> ThreadingHTTPServer:
    """phone_test_identity: the emulator test instance — a request with no Tailscale header counts as that login,
    and it refuses to run on your real phone store."""
    import phone
    if phone_test_identity and phone.DIR.resolve() == phone.DEFAULT_DIR.resolve():
        raise SystemExit("--phone-test-identity needs PHONE_HELPER_DIR set to a throwaway store")
    httpd = ThreadingHTTPServer(("127.0.0.1", config()["port"] if port is None else port), Handler)
    httpd.daemon_threads = True
    httpd.phone_test_identity = phone_test_identity
    if not block:
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd
    try:
        phone.scrub_stale()                     # a text's words never outlive their order, even across a restart
    except Exception as e:
        print(f"phone scrub on start failed: {e}", flush=True)
    if not config()["tailnet_login"] and not phone_test_identity:
        print("WARNING: tailnet_login is empty in config.json, so every request is refused.", flush=True)
    print(f"phone helper server on 127.0.0.1:{httpd.server_address[1]}", flush=True)
    httpd.serve_forever()
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="phone helper server")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--phone-test-identity", default="")
    a = ap.parse_args(argv)
    serve(a.port, phone_test_identity=a.phone_test_identity)
    return 0


if __name__ == "__main__":
    sys.exit(main())
