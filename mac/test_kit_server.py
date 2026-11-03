"""The kit's own pieces: the standalone server fails closed, and the voice tools are all there."""
import asyncio
import http.client
import json
import tempfile
import unittest
from pathlib import Path

import notify as N
import phone as P
import phone_server as A


class KitServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = (A.ROOT, A.CONFIG_FILE, N.ROOT, N.ACTIVITY, P.DIR)
        A.ROOT, A.CONFIG_FILE = root, root / "config.json"
        N.ROOT, N.ACTIVITY = root, root / "activity.jsonl"
        P.DIR = root / "phone"

    def tearDown(self):
        A.ROOT, A.CONFIG_FILE, N.ROOT, N.ACTIVITY, P.DIR = self._saved
        self.tmp.cleanup()

    def status(self, login):
        httpd = A.serve(port=0, block=False)
        try:
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            body = json.dumps({"code": P.pair_code("my-phone")}).encode()
            c.request("POST", "/phone/api/pair", body=body,
                      headers={"Content-Length": str(len(body)), **({"Tailscale-User-Login": login} if login else {})})
            st = c.getresponse().status
            c.close()
            return st
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_no_login_configured_refuses_everyone(self):
        self.assertEqual(self.status("you@example.com"), 403)

    def test_only_the_configured_login_pairs(self):
        A.CONFIG_FILE.write_text(json.dumps({"tailnet_login": "you@example.com"}))
        self.assertEqual(self.status(""), 403)
        self.assertEqual(self.status("someone@else.com"), 403)
        self.assertEqual(self.status("you@example.com"), 200)

    def test_listens_on_loopback_only(self):
        httpd = A.serve(port=0, block=False)
        try:
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
        finally:
            httpd.shutdown(); httpd.server_close()


class VoiceTools(unittest.TestCase):
    def test_all_tools_are_offered(self):
        try:
            import phone_mcp
        except ImportError:
            self.skipTest("pip install mcp")
        names = {t.name for t in asyncio.run(phone_mcp.mcp.list_tools())}
        self.assertEqual(names, {"phone_find_contact", "phone_call", "phone_text", "phone_inbox", "phone_rundown",
                                 "phone_status"})


if __name__ == "__main__":
    unittest.main()
