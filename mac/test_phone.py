"""Phone order channel (phone.py + the /phone routes on the approvals daemon; phone-helper docs/design.md §2, §8)."""
import http.client
import io
import json
import os
import re
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

import phone_server as A
import notify as N
import phone as P

LOGIN = "you@example.com"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = (A.ROOT, A.STORE, A.CONFIG_FILE, N.ROOT, N.ACTIVITY, N.KNOWN, N.send, P.DIR, P.POLL_S)
        A.ROOT, A.STORE, A.CONFIG_FILE = root, root / "approvals", root / "approvals.json"
        N.ROOT, N.ACTIVITY, N.KNOWN = root, root / "activity.jsonl", root / "known.json"
        N.send = lambda text: True
        P.DIR = root / "phone"
        P.POLL_S = 0.05
        self.config(tailnet_login=LOGIN, port=0)

    def tearDown(self):
        A.ROOT, A.STORE, A.CONFIG_FILE, N.ROOT, N.ACTIVITY, N.KNOWN, N.send, P.DIR, P.POLL_S = self._saved
        self.tmp.cleanup()

    def config(self, **kw):
        c = json.loads(A.CONFIG_FILE.read_text()) if A.CONFIG_FILE.exists() else {}
        c.update(kw)
        A.CONFIG_FILE.write_text(json.dumps(c))

    def paired(self, name="my-phone"):
        return P.pair(P.pair_code(name), name, "0.1.0")

    def events(self):
        return [e["ev"] for e in P.read_log(500)]


class Store(Base):
    def test_pairing_code_works_once_and_only_hashes_are_kept(self):
        code = P.pair_code("my-phone")
        device, key = P.pair(code, "my-phone", "0.1.0")
        self.assertTrue(P.authenticate(device, key))
        self.assertIsNone(P.pair(code))                       # used
        for f in P.DIR.rglob("*"):
            if f.is_file():
                text = f.read_text()
                self.assertNotIn(code, text, f.name)
                self.assertNotIn(key, text, f.name)
        self.assertEqual(stat.S_IMODE(os.stat(P.DIR).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(P.DIR / "devices.json").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(P.DIR / "phone.log").st_mode), 0o600)

    def test_expired_code_refused(self):
        code = P.pair_code("my-phone")
        p = json.loads((P.DIR / "pairing.json").read_text())
        p["codes"][0]["expires"] = time.time() - 1
        (P.DIR / "pairing.json").write_text(json.dumps(p))
        self.assertIsNone(P.pair(code))

    def test_long_code_life_is_capped_at_twelve_hours(self):
        P.pair_code("my-phone", life_s=8 * 3600)
        P.pair_code("my-phone", life_s=99 * 3600)
        left = [c["expires"] - time.time() for c in json.loads((P.DIR / "pairing.json").read_text())["codes"]]
        self.assertAlmostEqual(left[0], 8 * 3600, delta=5)
        self.assertAlmostEqual(left[1], P.CODE_MAX_LIFE_S, delta=5)
        self.assertAlmostEqual(left[1], 12 * 3600, delta=5)

    def test_five_wrong_tries_kill_open_codes(self):
        code = P.pair_code("my-phone")
        wrong = "00000000" if code != "00000000" else "11111111"
        for _ in range(P.CODE_MAX_WRONG):
            self.assertIsNone(P.pair(wrong))
        self.assertIsNone(P.pair(code))

    def test_pairing_again_replaces_the_old_key_and_revoke_ends_a_device(self):
        d1, k1 = self.paired()
        d2, k2 = self.paired()
        self.assertFalse(P.authenticate(d1, k1))
        self.assertTrue(P.authenticate(d2, k2))
        self.assertTrue(P.revoke(d2))
        self.assertFalse(P.authenticate(d2, k2))
        self.assertIsNone(P.default_device())

    def test_order_delivered_once_with_relative_ttl(self):
        device, _ = self.paired()
        o = P.send("find", {"name": "Lena", "action": "call"})
        got = P.next_order(device)
        self.assertEqual(got["id"], o["id"])
        self.assertNotIn("state", got)
        self.assertTrue(0 < got["ttl_s"] <= 10)
        self.assertIsNone(P.next_order(device))
        self.assertEqual(P.load_order(o["id"])["state"], "delivered")

    def test_orders_go_only_to_their_device(self):
        d1, _ = self.paired("my-phone")
        d2, _ = self.paired("other")
        P.send("ping", device=d1)
        self.assertIsNone(P.next_order(d2))
        self.assertIsNotNone(P.next_order(d1))

    def test_call_with_under_70_s_left_is_not_delivered_and_expired_orders_are_marked(self):
        device, _ = self.paired()
        P.send("call", {"find": "f_1", "pick": "p_1"}, life_s=60)
        self.assertIsNone(P.next_order(device))
        o = P.send("ping", life_s=0.01)
        time.sleep(0.05)
        self.assertIsNone(P.next_order(device))
        self.assertEqual(P.load_order(o["id"])["state"], "expired")
        self.assertIn("expired", self.events())

    def test_result_with_a_full_number_refused(self):
        device, _ = self.paired()
        o = P.send("find", {"name": "Lena"})
        P.next_order(device)
        for number in ("0412 345 678", "+61412345678", "(07) 4681-1234", "0412/345/678", "０４１２３４５６７８"):
            res = {"id": o["id"], "ok": True, "outcome": "done",
                   "data": {"matches": [{"pick": "p_2", "name": "Lena", "label": number, "last3": "678"}]}}
            self.assertEqual(P.answer(device, res)[0], 400, number)
        leaks = [{"n": 61412345678}, {"matches": [{"pick": "0412 345 678"}]}, {"find": "f_0412345678"},
                 {"0412345678": "key"}, {"deep": [{"x": {"pick": "p_412345678"}}]}]
        for data in leaks:
            self.assertEqual(P.answer(device, {"id": o["id"], "ok": True, "outcome": "done", "data": data})[0], 400, data)
        res["data"]["matches"][0]["label"] = "Mobile"
        res.update(at=1762060001.2, app_version="0.1.0")        # timestamps and short ids are fine
        res["data"]["find"] = "f_p9"
        self.assertEqual(P.answer(device, res), (200, ""))
        for f in P.DIR.rglob("*"):                               # independent of the check under test
            if f.is_file():
                self.assertNotIn("412345678", re.sub(r"[\s/().+-]", "", f.read_text()), f.name)

    def test_wait_result_times_out_and_late_answer_is_refused(self):
        device, _ = self.paired()
        o = P.send("ping", life_s=0.5)
        P.next_order(device)
        t = time.time()
        self.assertIsNone(P.wait_result(o["id"]))
        self.assertLess(time.time() - t, 2)
        self.assertEqual(P.answer(device, {"id": o["id"], "ok": True, "outcome": "done"})[0], 409)
        self.assertIn("expired", self.events())

    def test_a_shorter_wait_leaves_the_order_live(self):
        device, _ = self.paired()
        o = P.send("call", {"find": "f_p9", "pick": "p_2"})
        P.next_order(device)
        self.assertIsNone(P.wait_result(o["id"], timeout_s=0.2))
        self.assertEqual(P.load_order(o["id"])["state"], "delivered")    # the phone may still act: so may its answer
        self.assertEqual(P.answer(device, {"id": o["id"], "ok": True, "outcome": "dialled"}), (200, ""))
        self.assertEqual(P.wait_result(o["id"], timeout_s=0.2)["outcome"], "dialled")

    def test_revoked_device_gets_no_order_and_bad_order_files_are_skipped(self):
        device, _ = self.paired()
        (P.DIR / "orders" / "o_broken.json").write_text(json.dumps({"device": device, "state": "open"}))
        P.send("ping", device=device)
        self.assertEqual(P.next_order(device)["kind"], "ping")
        P.send("ping", device=device)
        P.revoke(device)
        self.assertIsNone(P.next_order(device))
        self.assertEqual(stat.S_IMODE(os.stat(P.DIR / "lock").st_mode), 0o600)

    def test_numbers_under_time_names_commas_and_odd_clocks_refused_or_dropped(self):
        device, _ = self.paired()
        o = P.send("find", {"name": "Lena"})
        P.next_order(device)
        ok = {"id": o["id"], "ok": True, "outcome": "done"}
        for data in ({"matches": [{"sent_at": 61412345678}]}, {"at": 1800123456}, {"label": "0412,345,678"},
                     {"label": "0412_345_678"}):
            self.assertEqual(P.answer(device, {**ok, "data": data})[0], 400, data)
        self.assertEqual(P.answer(device, {**ok, "at": 61412345678}), (200, ""))   # a mad clock is dropped, not kept
        self.assertNotIn("at", P.wait_result(o["id"], timeout_s=0.1))
        self.assertNotIn("412345678", "".join(f.read_text() for f in P.DIR.rglob("*") if f.is_file()))

    def test_deep_nesting_and_bad_matches_refused_before_anything_is_written(self):
        device, _ = self.paired()
        o = P.send("find", {"name": "Lena"})
        P.next_order(device)
        deep = {}
        for _ in range(900):
            deep = {"x": deep}
        for data in (deep, {"matches": 5}, {"matches": ["Lena"]}):
            self.assertEqual(P.answer(device, {"id": o["id"], "ok": True, "outcome": "done", "data": data})[0], 400)
        self.assertEqual(P.load_order(o["id"])["state"], "delivered")      # still answerable
        self.assertEqual(P.answer(device, {"id": o["id"], "ok": True, "outcome": "done", "data": {"matches": []}}),
                         (200, ""))

    def test_answer_after_the_order_life_is_refused_even_if_nobody_waited(self):
        device, _ = self.paired()
        o = P.send("ping")
        self.assertEqual(P.next_order(device)["id"], o["id"])
        stored = P.load_order(o["id"])
        stored["expires"] = time.time() - 1                      # its life ran out while delivered
        P._write(P._order_path(o["id"]), stored)
        self.assertEqual(P.answer(device, {"id": o["id"], "ok": True, "outcome": "done"})[0], 409)
        self.assertEqual(P.load_order(o["id"])["state"], "expired")
        self.assertIn("expired", self.events())

    def test_ids_are_letters_and_orders_never_carry_a_number(self):
        device, _ = self.paired()
        self.assertRegex(device, r"^d_[A-Za-z]+$")
        self.assertRegex(P.send("ping")["id"], r"^o_[A-Za-z]+$")
        self.assertIsNone(P.DEVICE_ID.match("d_0412345678"))
        self.assertIsNone(P.ORDER_ID.match("o_abcd\n"))
        with self.assertRaises(ValueError):
            P.send("find", {"name": "0412 345 678"})


class Web(Base):
    def setUp(self):
        super().setUp()
        self.httpd = A.serve(port=0, block=False)
        self.httpd.daemon_threads = False          # server_close() then waits for every handler thread
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        super().tearDown()

    def http(self, method, path, body=None, login=LOGIN, device=None, key=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=40)
        h = dict(headers or {})
        if login:
            h["Tailscale-User-Login"] = login
        if device:
            h["X-Phone-Device"] = device
        if key:
            h["Authorization"] = f"Bearer {key}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None and "Content-Length" not in h:
            h["Content-Length"] = str(len(data))
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, (json.loads(out) if out else None)

    def pair_http(self, name="my-phone"):
        code = P.pair_code(name)
        st, body = self.http("POST", "/phone/api/pair", {"code": code, "name": name, "app_version": "0.1.0"})
        self.assertEqual(st, 200)
        return body["device"], body["key"]

    def test_order_delivered_and_answered(self):
        device, key = self.pair_http()
        o = P.send("ping")
        got = {}
        waiter = threading.Thread(target=lambda: got.update(r=P.wait_result(o["id"])))
        waiter.start()
        st, order = self.http("GET", "/phone/api/next?wait=5", device=device, key=key,
                              headers={"X-App-Version": "0.1.0", "X-Phone-Net": "wifi"})
        self.assertEqual((st, order["id"], order["kind"]), (200, o["id"], "ping"))
        res = {"v": 1, "id": o["id"], "kind": "ping", "ok": True, "outcome": "done",
               "data": {"version": "0.1.0", "sdk": 34}, "error": "", "app_version": "0.1.0", "at": time.time()}
        self.assertEqual(self.http("POST", "/phone/api/result", res, device=device, key=key), (200, {"ok": True}))
        waiter.join(5)
        self.assertEqual(got["r"]["data"]["version"], "0.1.0")
        self.assertEqual(self.http("POST", "/phone/api/result", res, device=device, key=key)[0], 409)  # answered
        d = P.devices()[device]
        self.assertEqual((d["app_version"], d["net"]), ("0.1.0", "wifi"))
        self.assertGreater(d["last_seen"], time.time() - 10)
        self.assertEqual(self.events()[-1], "ping")

    def test_long_poll_answers_the_moment_an_order_is_queued(self):
        device, key = self.pair_http()
        threading.Timer(0.5, lambda: P.send("ping")).start()
        t = time.time()
        st, order = self.http("GET", "/phone/api/next?wait=10", device=device, key=key)
        self.assertEqual((st, order["kind"]), (200, "ping"))
        self.assertLess(time.time() - t, 3)

    def test_long_poll_times_out_cleanly(self):
        device, key = self.pair_http()
        t = time.time()
        self.assertEqual(self.http("GET", "/phone/api/next?wait=1", device=device, key=key), (204, None))
        self.assertTrue(0.9 <= time.time() - t < 3)
        # and the connection is still good for the next poll
        P.send("ping")
        self.assertEqual(self.http("GET", "/phone/api/next?wait=1", device=device, key=key)[0], 200)

    def test_a_newer_long_poll_replaces_an_older_one(self):
        device, key = self.pair_http()
        old, start = {}, time.time()
        t = threading.Thread(target=lambda: old.update(r=self.http("GET", "/phone/api/next?wait=10", device=device,
                                                                       key=key), at=time.time()))
        t.start()
        time.sleep(0.5)                                          # the old poll is waiting (a dead mobile link)
        threading.Timer(0.5, lambda: P.send("ping")).start()
        st, order = self.http("GET", "/phone/api/next?wait=10", device=device, key=key)
        t.join(10)
        self.assertEqual((st, order["kind"]), (200, "ping"))     # the fresh connection gets the order
        self.assertEqual(old["r"], (204, None))
        self.assertLess(old["at"] - start, 3)                    # ended at once, not after its 10 s

    def test_a_long_poll_whose_phone_hung_up_never_takes_an_order(self):
        device, key = self.pair_http()
        import socket
        s = socket.create_connection(("127.0.0.1", self.port))   # the app's poll, then the app is killed (an update)
        s.sendall((f"GET /phone/api/next?wait=10 HTTP/1.1\r\nHost: x\r\nTailscale-User-Login: {LOGIN}\r\n"
                   f"X-Phone-Device: {device}\r\nAuthorization: Bearer {key}\r\n\r\n").encode())
        time.sleep(0.5)
        s.close()
        time.sleep(0.5)
        o = P.send("ping")                                       # sent while the restarted app is not polling yet
        time.sleep(1)
        st, order = self.http("GET", "/phone/api/next?wait=5", device=device, key=key)
        self.assertEqual((st, order["id"]), (200, o["id"]))      # the restarted app's first poll gets it

    def test_negative_length_refused(self):
        device, key = self.pair_http()
        self.assertEqual(self.http("POST", "/phone/api/result", device=device, key=key, raw=b"{}",
                                   headers={"Content-Length": "-1"})[0], 400)

    def test_wait_is_capped_at_25_s(self):
        device, key = self.pair_http()
        saved = P.MAX_WAIT_S
        P.MAX_WAIT_S = 1
        try:
            t = time.time()
            self.assertEqual(self.http("GET", "/phone/api/next?wait=600", device=device, key=key)[0], 204)
            self.assertLess(time.time() - t, 3)
        finally:
            P.MAX_WAIT_S = saved

    def test_refused_without_tailnet_identity(self):
        device, key = self.pair_http()
        P.send("ping")
        for login in (None, "mallory@example.com"):
            self.assertEqual(self.http("GET", "/phone/api/next?wait=0", login=login, device=device, key=key)[0], 403)
            self.assertEqual(self.http("POST", "/phone/api/pair", {"code": P.pair_code("x")}, login=login)[0], 403)
            self.assertEqual(self.http("POST", "/phone/api/result", {}, login=login, device=device, key=key)[0], 403)
            self.assertEqual(self.http("GET", "/phone/nothing", login=login)[0], 403)
        self.assertEqual(P.next_order(device)["kind"], "ping")      # never delivered to the refused requests
        self.assertIn("auth_refused", self.events())
        self.assertTrue(any(e["kind"] == "auth_fail" for e in N.events(0)))

    def test_fails_closed_when_tailnet_login_is_not_set(self):
        device, key = self.pair_http()
        self.config(tailnet_login="")
        self.assertEqual(self.http("GET", "/phone/api/next?wait=0", device=device, key=key)[0], 403)
        self.assertEqual(self.http("GET", "/phone/api/next?wait=0", login=None, device=device, key=key)[0], 403)

    def test_refused_with_a_wrong_or_missing_device_key(self):
        device, key = self.pair_http()
        P.send("ping")
        cases = [dict(device=device, key=None), dict(device=device, key="x" * 43), dict(device=None, key=key),
                 dict(device="d_nobody", key=key), dict(device="../../etc", key=key)]
        for c in cases:
            self.assertEqual(self.http("GET", "/phone/api/next?wait=0", **c)[0], 401, c)
            self.assertEqual(self.http("POST", "/phone/api/result", {"id": "o_abcd", "ok": True, "outcome": "done"},
                                       **c)[0], 401, c)
        self.assertEqual(self.http("GET", "/phone/api/next?wait=0", device=device,
                                   headers={"Authorization": key})[0], 401)    # not a Bearer header
        P.revoke(device)
        self.assertEqual(self.http("GET", "/phone/api/next?wait=0", device=device, key=key)[0], 401)
        self.assertEqual(self.events().count("auth_refused"), len(cases) * 2 + 2)
        self.http("GET", "/phone/api/next?wait=0", device="d_0412345678", key=key)
        self.assertNotIn("412345678", (P.DIR / "phone.log").read_text())   # a numbery device id is never logged

    def test_key_from_one_device_does_not_open_another(self):
        d1, k1 = self.pair_http("my-phone")
        d2, k2 = self.pair_http("tablet")
        self.assertEqual(self.http("GET", "/phone/api/next?wait=0", device=d2, key=k1)[0], 401)

    def test_oversize_body_refused_without_reading_it(self):
        device, key = self.pair_http()
        t = time.time()
        # claims 1 MB, sends 2 bytes: if the daemon tried to read it, this would hang until the client timeout
        st, _ = self.http("POST", "/phone/api/result", device=device, key=key, raw=b"{}",
                          headers={"Content-Length": str(1024 * 1024)})
        self.assertEqual(st, 413)
        self.assertLess(time.time() - t, 3)
        st, _ = self.http("POST", "/phone/api/pair", raw=b"{}", headers={"Content-Length": str(P.MAX_BODY + 1)})
        self.assertEqual(st, 413)
        self.assertIn("refused_body", self.events())

    def test_bad_shapes_and_routes(self):
        device, key = self.pair_http()
        self.assertEqual(self.http("POST", "/phone/api/result", raw=b"not json", device=device, key=key)[0], 400)
        self.assertEqual(self.http("POST", "/phone/api/result", {"id": "o_abcd", "ok": "yes", "outcome": "done"},
                                   device=device, key=key)[0], 400)
        self.assertEqual(self.http("POST", "/phone/api/result", {"id": "../x", "ok": True, "outcome": "done"},
                                   device=device, key=key)[0], 400)
        self.assertEqual(self.http("POST", "/phone/api/result", {"id": "o_nosuch", "ok": True, "outcome": "done"},
                                   device=device, key=key)[0], 409)
        self.assertEqual(self.http("GET", "/phone/api/result", device=device, key=key)[0], 405)
        self.assertEqual(self.http("POST", "/phone/api/next", {}, device=device, key=key)[0], 405)
        self.assertEqual(self.http("GET", "/phone/api/other", device=device, key=key)[0], 404)
        self.assertEqual(self.http("POST", "/phone/api/pair", {"code": "12345678"})[0], 403)
        self.assertIn("pair_refused", self.events())

    def test_result_for_an_order_never_delivered_is_refused(self):
        device, key = self.pair_http()
        o = P.send("ping")
        self.assertEqual(self.http("POST", "/phone/api/result", {"id": o["id"], "ok": True, "outcome": "done"},
                                   device=device, key=key)[0], 409)

    def test_find_result_logged_with_no_full_number(self):
        device, key = self.pair_http()
        o = P.send("find", {"name": "Lena", "action": "call"})
        self.http("GET", "/phone/api/next?wait=1", device=device, key=key)
        res = {"id": o["id"], "ok": True, "outcome": "done", "data": {"find": "f_p9", "matches": [
            {"pick": "p_2", "name": "Lena Brennan", "label": "Mobile", "last3": "452", "callable": True}]}}
        self.assertEqual(self.http("POST", "/phone/api/result", res, device=device, key=key)[0], 200)
        line = P.read_log(1)[0]
        self.assertEqual((line["ev"], line["who"], line["matches"][0]["last3"]), ("find", "Lena", "452"))
        self.assertNotIn("pick", line["matches"][0])
        blob = "".join(f.read_text() for f in P.DIR.rglob("*") if f.is_file())
        self.assertNotIn(key, blob)
        for raw in (P.DIR / "phone.log").read_text().splitlines():
            line = json.loads(raw); line.pop("at")
            self.assertIsNone(P.NUMBER_LIKE.search(json.dumps(P._flatten(line))), line)

    def test_other_routes_untouched(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/nothing", headers={"Tailscale-User-Login": LOGIN})
        self.assertEqual(c.getresponse().status, 404)
        c.close()


class TestIdentity(Base):
    """The emulator harness daemon (design §9): a second instance, test login only, /phone only."""

    def setUp(self):
        super().setUp()
        self.httpd = A.serve(port=0, block=False, phone_test_identity="emulator@test")
        self.httpd.daemon_threads = False          # server_close() then waits for every handler thread
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers or {})
        if data is not None:
            h["Content-Length"] = str(len(data))
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_no_header_counts_as_the_test_login_and_nothing_else_is_served(self):
        self.assertEqual(self.req("POST", "/phone/api/pair", {"code": P.pair_code("emu", test=True)}), 200)
        self.assertEqual(self.req("POST", "/phone/api/pair", {"code": P.pair_code("emu")},
                                  headers={"Tailscale-User-Login": LOGIN}), 403)   # only the test login
        self.assertEqual(self.req("GET", "/shop"), 404)
        self.assertEqual(self.req("GET", "/lena"), 404)
        self.assertEqual(self.req("GET", "/a/x/y"), 404)

    def test_refuses_to_run_on_the_live_store(self):
        P.DIR = P.DEFAULT_DIR
        with self.assertRaises(SystemExit):
            A.serve(port=0, block=False, phone_test_identity="emulator@test")


class VoiceFixture(Base):
    """A paired, connected phone and a fake phone that answers finds (shared by the voice-tool tests)."""
    LENA = {"pick": "p_a", "name": "Lena Brennan", "label": "Mobile", "last3": "452", "callable": True, "why_not": ""}
    LENNY = {"pick": "p_b", "name": "Lenny Smith", "label": "Home", "last3": "781", "callable": True, "why_not": ""}

    def setUp(self):
        super().setUp()
        self._saved_v = (P.CALL_CONFIRM_S, P.FIND_WAIT_S, P.TOKEN_LIFE_S)
        P.CALL_CONFIRM_S, P.FIND_WAIT_S = 0.2, 3
        self.device, _ = self.paired()
        P.touch(self.device)

    def tearDown(self):
        P.CALL_CONFIRM_S, P.FIND_WAIT_S, P.TOKEN_LIFE_S = self._saved_v
        super().tearDown()

    def phone(self, matches, find="f_x", ok=True, error=""):
        """A fake phone that answers the next find with these matches."""
        def run():
            end = time.time() + 3
            while time.time() < end:
                o = P.next_order(self.device)
                if o and o["kind"] == "find":                           # a call it holds (delivered) stays pending
                    P.answer(self.device, {"v": 1, "id": o["id"], "ok": ok, "outcome": "done" if ok else "failed",
                                           "data": {"find": find, "matches": matches}, "error": error})
                    return
                time.sleep(0.02)
        t = threading.Thread(target=run)
        t.start()
        return t

    def find(self, matches, name="Lena", action="call", **kw):
        t = self.phone(matches, **kw)
        out = P.find_contact(name, action)
        t.join(5)
        return out

    @staticmethod
    def tokens(out):
        return re.findall(r"token ([a-z0-9]{6})", out)

    def calls(self):
        return [json.loads(f.read_text()) for f in (P.DIR / "orders").glob("o_*.json")
                if json.loads(f.read_text())["kind"] == "call"]

    def take_calls(self):
        """The fake phone takes and answers every queued call, so none is pending any more."""
        while (o := P.next_order(self.device)):
            P.answer(self.device, {"v": 1, "id": o["id"], "ok": True, "outcome": "dialled", "data": {}})


class VoiceTools(VoiceFixture):
    """phone_find_contact / phone_call (design §4): 60 s one-use tokens bound to the phone's pick, others cancelled,
    rate cap, phone offline, and every call and refusal in the phone log."""

    def test_find_reads_back_and_the_token_rings_once(self):
        out = self.find([self.LENA])
        self.assertIn("Read back: Lena Brennan, mobile ending 4-5-2", out)
        self.assertIn("ONLY after the user says yes", out)
        self.assertIn("hang up", out)
        [tok] = self.tokens(out)
        self.assertTrue(set(tok) <= set(P.TOKEN_ALPHABET))
        said = P.call(tok)
        self.assertIn("hang up", said)
        self.assertIn("end it on their phone himself", said)                  # 4 Nov: voice Claude's own hang-up didn't
        [c] = self.calls()
        self.assertEqual((c["args"], c["device"]), ({"find": "f_x", "pick": "p_a"}, self.device))
        self.assertIn("already been used", P.call(tok))                        # reuse
        self.assertEqual(len(self.calls()), 1)
        evs = P.read_log(50)
        self.assertEqual([e["ev"] for e in evs if e["ev"] in ("find", "token", "call", "token_refused")],
                         ["find", "token", "call", "token_refused"])
        call = next(e for e in evs if e["ev"] == "call")
        self.assertEqual((call["name"], call["last3"], call["token"]), ("Lena Brennan", "452", tok[:2]))
        self.assertEqual(next(e for e in evs if e["ev"] == "token_refused")["why"], "used")
        for f in P.DIR.rglob("*"):                                             # only the hash and 2 letters kept
            if f.is_file():
                self.assertNotIn(tok, f.read_text(), f.name)
        acts = [json.loads(x) for x in N.ACTIVITY.read_text().splitlines()]
        self.assertTrue(any(a.get("ev") == "call" for a in acts))

    def test_token_expires_after_its_life(self):
        P.TOKEN_LIFE_S = 0.2
        [tok] = self.tokens(self.find([self.LENA]))
        time.sleep(0.3)
        self.assertIn("run out", P.call(tok))
        self.assertEqual(self.calls(), [])
        self.assertEqual(P.read_log(1)[0]["why"], "expired")

    def test_wrong_or_empty_token_refused(self):
        self.find([self.LENA])
        for bad in ("zzzzzz", "", "../../x", "ZZ ZZ"):
            self.assertIn("doesn't match", P.call(bad))
            self.assertEqual(P.read_log(1)[0], {**P.read_log(1)[0], "ev": "token_refused", "why": "wrong"})
        self.assertEqual(self.calls(), [])

    def test_using_one_token_cancels_the_others(self):
        out = self.find([self.LENA, self.LENNY], name="Len")
        self.assertIn("Ask the user which one", out)
        self.assertIn("ending 7-8-1", out)
        t1, t2 = self.tokens(out)
        self.assertIn("hang up", P.call(t2))
        self.assertIn("cancelled", P.call(t1))
        [c] = self.calls()
        self.assertEqual(c["args"]["pick"], "p_b")
        self.assertEqual(P.read_log(1)[0]["why"], "cancelled")

    def test_a_new_find_cancels_older_tokens(self):
        [old] = self.tokens(self.find([self.LENA]))
        [new] = self.tokens(self.find([self.LENNY], name="Lenny"))
        self.assertIn("cancelled", P.call(old))
        self.assertIn("hang up", P.call(new))
        self.assertIn("token_cancelled", self.events())

    def test_token_bound_to_its_action(self):
        self.assertIn("not 'email'", P.find_contact("Lena", "email"))
        t = self.phone([self.LENA])
        [tok] = self.tokens(P.find_contact("Lena", "text", "on my way"))
        t.join(5)
        self.assertIn("not a call", P.call(tok))
        self.assertEqual(self.calls(), [])

    def test_rate_cap_ten_in_ten_minutes_and_ten_a_day(self):
        for _ in range(10):
            [tok] = self.tokens(self.find([self.LENA]))
            self.assertIn("hang up", P.call(tok))
            self.take_calls()
        out = self.find([self.LENA])                                          # 11th: refused before it asks the phone
        self.assertIn("most I'm allowed", out)
        self.assertEqual(self.tokens(out), [])
        self.assertEqual(P.read_log(1)[0]["why"], "rate cap 10 in 10 minutes")
        t = json.loads((P.DIR / "tokens.json").read_text())                    # a token minted just before the cap
        t["queued"] = t["queued"][:9]
        (P.DIR / "tokens.json").write_text(json.dumps(t))
        [tok] = self.tokens(self.find([self.LENA]))
        t = json.loads((P.DIR / "tokens.json").read_text())
        t["queued"].append(time.time())
        (P.DIR / "tokens.json").write_text(json.dumps(t))
        self.assertIn("most I'm allowed", P.call(tok))
        self.assertEqual(P.read_log(1)[0]["why"], "rate")
        self.assertEqual(len(self.calls()), 10)
        t["queued"] = [time.time() - 3600 - i for i in range(99)]               # 99 in the last day, none in 10 min
        t["tokens"] = []
        (P.DIR / "tokens.json").write_text(json.dumps(t))
        self.assertEqual(len(self.tokens(self.find([self.LENA]))), 1)          # the 100th may still go
        t = json.loads((P.DIR / "tokens.json").read_text())
        t["queued"] = [time.time() - 3600 - i for i in range(100)]              # 100 in the last day
        (P.DIR / "tokens.json").write_text(json.dumps(t))
        self.assertIn("most I'm allowed", self.find([self.LENA]))
        self.assertEqual(P.read_log(1)[0]["why"], "rate cap 100 in a day")

    def test_one_call_on_its_way_at_a_time(self):
        [tok] = self.tokens(self.find([self.LENA]))
        P.call(tok)
        [tok] = self.tokens(self.find([self.LENNY], name="Lenny"))
        self.assertIn("already on its way", P.call(tok))
        self.assertEqual(len(self.calls()), 1)

    def test_phone_offline_gives_a_plain_answer_and_is_logged(self):
        d = json.loads((P.DIR / "devices.json").read_text())
        d[self.device]["last_seen"] = time.time() - 300
        (P.DIR / "devices.json").write_text(json.dumps(d))
        out = P.find_contact("Lena")
        self.assertIn("isn't connected right now, last seen 5 min ago", out)
        self.assertEqual(list((P.DIR / "orders").glob("*.json")), [])          # nothing queued
        self.assertEqual(P.read_log(1)[0]["ev"], "offline")
        P.touch(self.device)                                                   # back, a token made, then gone again
        [tok] = self.tokens(self.find([self.LENA]))
        d = json.loads((P.DIR / "devices.json").read_text())
        d[self.device]["last_seen"] = time.time() - 300
        (P.DIR / "devices.json").write_text(json.dumps(d))
        self.assertIn("isn't connected", P.call(tok))
        self.assertEqual(self.calls(), [])
        self.assertEqual(P.read_log(1)[0]["why"], "offline")

    def test_no_answer_from_the_phone(self):
        P.FIND_WAIT_S = 0.3
        self.assertIn("didn't answer in time", P.find_contact("Lena"))       # paired, online, nobody answers
        self.assertEqual(P.read_log(1)[0]["why"], "no answer")

    def test_revoked_phone(self):
        [tok] = self.tokens(self.find([self.LENA]))
        P.revoke(self.device)
        self.assertIn("No phone is paired", P.find_contact("Lena"))
        self.assertIn("isn't paired with the Mac any more", P.call(tok))
        self.assertEqual(self.calls(), [])

    def test_not_callable_numbers_and_spoken_numbers_get_no_token(self):
        bad = {**self.LENA, "callable": False, "why_not": "it's an emergency number"}
        out = self.find([bad])
        self.assertEqual(self.tokens(out), [])
        self.assertIn("emergency number", out)
        self.assertIn("never try another way", out)
        self.assertIn("never a spoken number", P.find_contact("0412 345 678"))
        self.assertIn("no saved contact on the phone called Zed", self.find([], name="Zed"))
        self.assertIn("couldn't look", self.find([], ok=False, error="not allowed to read contacts"))
        self.assertEqual(P.read_log(1)[0]["ev"], "failed")


class Status(Base):
    """phone_status (design §2.4): last seen, network and app version from the phone's own /next, in plain words."""
    def test_never_paired_connected_and_gone_quiet(self):
        self.assertEqual(P.status(), "No phone is paired with the Mac yet.")
        device, _ = self.paired()
        P.touch(device, "0.4.0", "cellular")
        out = P.status()
        self.assertTrue(out.startswith("The phone is connected on mobile data, last seen "), out)
        self.assertIn("App version 0.4.0.", out)
        d = json.loads((P.DIR / "devices.json").read_text())
        d[device]["last_seen"] = time.time() - P.OFFLINE_AFTER_S - 5      # just past the line: not connected
        (P.DIR / "devices.json").write_text(json.dumps(d))
        self.assertIn("isn't connected right now, last seen 45 s ago on mobile data", P.status())
        d[device]["last_seen"] = time.time() - 3 * 3600
        d[device]["net"] = "wifi"
        (P.DIR / "devices.json").write_text(json.dumps(d))
        self.assertIn("last seen 3 h ago on wifi", P.status())
        self.assertEqual(self.events(), ["pair_code", "pair"])              # reading the status logs nothing
        self.assertNotIn("connected on", P.status())

    def test_cli_prints_it(self):
        self.paired()
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(P.main(["status"]), 0)
        self.assertIn("isn't connected right now, last seen never", out.getvalue())


class OutcomeFixture(VoiceFixture):
    def queue_call(self, match=None):
        [tok] = self.tokens(self.find([match or self.LENA]))
        self.assertIn("hang up", P.call(tok))
        return P.next_order(self.device)                                    # the phone takes it

    def answer(self, o, outcome, ok=False, error="", data=None):
        code, why = P.answer(self.device, {"v": 1, "id": o["id"], "ok": ok, "outcome": outcome,
                                           "data": data or {}, "error": error})
        self.assertEqual(code, 200, why)


class Outcomes(OutcomeFixture):
    """design §6.1, §8 (step outcomes-to-voice): the phone's call result is logged as dialled / cancelled / refused /
    failed with who it was for, and call_start says one that did not go through, once, on the user's next call."""

    def test_cancelled_call_is_said_once_on_the_next_call(self):
        o = self.queue_call()
        self.assertEqual(P.outcomes(), [])                                  # still on its way: nothing yet
        self.answer(o, "cancelled", error="the Claude call did not end within a minute")
        ev = P.read_log(1)[0]
        self.assertEqual((ev["ev"], ev["name"], ev["last3"], ev["label"]), ("cancelled", "Lena Brennan", "452", "Mobile"))
        [peek] = P.outcomes(mark=False)                                     # a peek does not use it up
        [line] = P.call_start_lines()
        self.assertIn(peek, line)
        self.assertTrue(line.startswith("PHONE"))
        self.assertIn("I didn't ring Lena Brennan (mobile ending 4-5-2)", line)
        self.assertIn("voice call was still going", line)
        self.assertEqual(P.call_start_lines(), [])                          # once
        self.assertEqual(P.outcomes(mark=False), [])

    def test_dialled_call_stays_quiet_and_is_logged_with_how(self):
        o = self.queue_call()
        self.answer(o, "dialled", ok=True, data={"after_s": 2.5, "how": "placeCall"})
        ev = P.read_log(1)[0]
        self.assertEqual((ev["ev"], ev["name"], ev["how"], ev["after_s"]), ("dialled", "Lena Brennan", "placeCall", 2.5))
        [line] = P.call_start_lines()                                       # sent-confirmations: said once
        self.assertIn("The call to Lena Brennan (mobile ending 4-5-2) went through at ", line)
        self.assertIn("can't tell whether they answered", line)
        self.assertEqual(P.call_start_lines(), [])
        self.assertTrue(all(w["told"] for w in P._watched()))

    def test_sent_text_says_the_phones_time_and_delivery(self):
        """sent-confirmations (3 Nov): the time is the phone's arrival clock plus sent_after_s; delivered when reported."""
        o = self.queue_call()
        at = time.time() - 30
        code, why = P.answer(self.device, {"v": 1, "id": o["id"], "ok": True, "outcome": "dialled", "at": at,
                                           "data": {"after_s": 2.5, "how": "placeCall", "sent_after_s": 12}})
        self.assertEqual(code, 200, why)
        [line] = P.outcomes()
        self.assertIn(f"went through at {P._clock(at + 12, time.time())}", line)
        t = time.mktime((2026, 11, 3, 16, 5, 0, 0, 0, -1))
        self.assertEqual(P._clock(t, t + 60), "4:05 pm")
        self.assertEqual(P._clock(t, t + 86400), "yesterday at 4:05 pm")
        self.assertEqual(P._clock(t, t + 3 * 86400), "on Tuesday at 4:05 pm")
        o = self.queue_call()
        self.answer(o, "sent", ok=True, data={"how": "sms", "delivered": True})
        self.assertEqual(P._watched()[-1]["action"], "call")                # a call watch: the text wording is below
        w = {"name": "Lena", "label": "Mobile", "last3": "452", "action": "text"}
        res = {"outcome": "sent", "received": t, "data": {"how": "sms", "delivered": True}}
        self.assertEqual(P._success_line(w, res, t + 60),
                         "Your text to Lena (mobile ending 4-5-2) sent at 4:05 pm, and the network says it was delivered.")
        res["data"]["delivered"] = None
        self.assertEqual(P._success_line(w, res, t + 60), "Your text to Lena (mobile ending 4-5-2) sent at 4:05 pm.")

    def test_failures_are_said_before_successes(self):
        ok, bad = self.queue_call(), None
        self.answer(ok, "dialled", ok=True, data={"after_s": 2.5, "how": "placeCall"})
        bad = self.queue_call(self.LENNY)
        self.answer(bad, "refused", error="it is an emergency number")
        lines = P.outcomes()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("The phone refused"))
        self.assertTrue(lines[1].startswith("The call to"))

    def test_late_report_after_offline_is_recorded_and_corrects_the_unsure_line(self):
        """The phone dialled but lost signal before it could report: the rundown says "not sure"; when the phone
        reconnects and resends, the late report is taken (never acted on) and said once as an update."""
        o = self.queue_call()
        order = P.load_order(o["id"])
        order["expires"] = time.time() - 1                                  # its life ran out unanswered
        P._write(P._order_path(o["id"]), order)
        [unsure] = P.outcomes()
        self.assertIn("I'm not sure the phone rang", unsure)
        code, why = P.answer(self.device, {"v": 1, "id": o["id"], "ok": True, "outcome": "dialled",
                                           "data": {"after_s": 2.5, "how": "placeCall"}})
        self.assertEqual(code, 200, why)
        self.assertTrue(P.read_log(1)[0].get("late") or P.load_order(o["id"]).get("late"))
        [upd] = P.outcomes()
        self.assertTrue(upd.startswith("Update on the one I wasn't sure of: The call to Lena Brennan"))
        self.assertEqual(P.outcomes(), [])
        code, _ = P.answer(self.device, {"v": 1, "id": o["id"], "ok": True, "outcome": "dialled", "data": {}})
        self.assertEqual(code, 409)                                         # a second report is refused

    def test_late_report_for_an_order_never_delivered_is_refused(self):
        o = self.queue_call()
        order = P.load_order(o["id"])
        order.update(state="expired")
        order.pop("delivered", None)
        P._write(P._order_path(o["id"]), order)
        code, _ = P.answer(self.device, {"v": 1, "id": o["id"], "ok": True, "outcome": "dialled", "data": {}})
        self.assertEqual(code, 409)

    def test_refused_and_failed_say_why(self):
        o = self.queue_call()
        self.answer(o, "refused", error="it is an emergency number")
        self.assertEqual(P.read_log(1)[0]["ev"], "refused")
        [r] = P.outcomes()
        self.assertIn("The phone refused to ring Lena Brennan (mobile ending 4-5-2): it is an emergency number. "
                      "Nothing was dialled.", r)
        o = self.queue_call(self.LENNY)
        self.answer(o, "failed", error="the phone would not dial by itself, so it shows a tap to call instead",
                    data={"after_s": 2.6, "how": "notification"})
        [f] = P.outcomes()
        self.assertIn("Lenny Smith (home ending 7-8-1) by itself", f)
        self.assertIn("'Tap to call' notification", f)
        o = self.queue_call()
        self.answer(o, "failed", error="the call did not start", data={"after_s": 2.5, "how": "placeCall"})
        self.assertIn("tried to ring Lena Brennan (mobile ending 4-5-2) but it didn't go through: the call did not "
                      "start.", P.outcomes()[0])

    def test_a_call_the_phone_never_answered_is_said_after_its_life(self):
        [tok] = self.tokens(self.find([self.LENA]))
        P.call(tok)
        [c] = self.calls()
        self.assertEqual(P.outcomes(mark=False), [])                        # life not over: still on its way
        c["expires"] = time.time() - 1                                      # its 90 s are up, the phone never said
        (P.DIR / "orders" / f"{c['id']}.json").write_text(json.dumps(c))
        [line] = P.call_start_lines()
        self.assertIn("I'm not sure the phone rang Lena Brennan", line)
        self.assertEqual(P.call_start_lines(), [])

    def test_two_outcomes_in_one_line_and_no_full_number_kept(self):
        self.answer(self.queue_call(), "cancelled", error="x")
        self.answer(self.queue_call(self.LENNY), "refused", error="no longer a saved number")
        [line] = P.call_start_lines()
        self.assertIn("Lena Brennan", line)
        self.assertIn("Lenny Smith", line)
        for w in P._watched():                                              # only clock readings are long numbers
            self.assertIsNone(P.NUMBER_LIKE.search(json.dumps({k: v for k, v in w.items() if k not in ("queued", "told")})))
        self.assertEqual(oct((P.DIR / "pending.json").stat().st_mode & 0o777), "0o600")

    def test_old_watches_are_dropped_after_three_days(self):
        self.answer(self.queue_call(), "cancelled", error="x")
        later = time.time() + P.TELL_KEEP_S + 60
        self.assertEqual(P.outcomes(now=later), [])
        self.assertEqual(P._watched(), [])

    def test_cli_outcomes_peek_then_mark(self):
        self.answer(self.queue_call(), "cancelled", error="x")
        import contextlib, io
        for _ in range(2):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(P.main(["outcomes", "--peek"]), 0)
            self.assertIn("I didn't ring Lena", buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            P.main(["outcomes"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            P.main(["outcomes"])
        self.assertEqual(buf.getvalue(), "")


class Texts(OutcomeFixture):
    """phone_text (design §11.2, §11.3, §11.6 pytest line): the token is bound to sha256(body) and refuses wrong,
    used, expired, a call token and a changed body; empty, over 300 or control-character bodies; the rate cap and
    one-pending rule are shared with calls; only args.body may hold digits; the body leaves the store on delivery
    and on expiry; log lines carry len and parts, never the words; the tool text for routes A and B."""
    BODY = "Running late, gate code 482913 at 5:30pm"            # digits on purpose: a body may hold them

    def find_text(self, matches=None, body=None, name="Lena", **kw):
        t = self.phone(matches or [self.LENA], **kw)
        out = P.find_contact(name, "text", self.BODY if body is None else body)
        t.join(5)
        return out

    def texts(self):
        return [json.loads(f.read_text()) for f in (P.DIR / "orders").glob("o_*.json")
                if json.loads(f.read_text())["kind"] == "text"]

    def route(self, send_sms):
        """The phone's ping reports whether SEND_SMS is granted (route A) or not (route B)."""
        o = P.send("ping", device=self.device)
        self.assertEqual(P.next_order(self.device)["id"], o["id"])
        code, why = P.answer(self.device, {"v": 1, "id": o["id"], "ok": True, "outcome": "done",
                                           "data": {"version": "0.5.0", "perms": {"send_sms": send_sms}}})
        self.assertEqual(code, 200, why)

    def assert_no_body_on_disk(self, body=None):
        for f in P.DIR.rglob("*"):
            if f.is_file():
                txt = f.read_text()
                for piece in ((body or self.BODY), "gate code", "482913"):
                    self.assertNotIn(piece, txt, f.name)
        for a in N.ACTIVITY.read_text().splitlines():
            self.assertNotIn("gate code", a)

    def test_find_reads_back_the_words_and_the_text_goes_once(self):
        out = self.find_text()
        self.assertIn(f"Read back: Lena Brennan, mobile ending 4-5-2: '{self.BODY}'", out)
        self.assertIn("Shall I send it?", out)
        self.assertIn("phone_text with the same text, word for word,", out)
        self.assertIn("ONLY after the user says yes", out)
        [tok] = self.tokens(out)
        said = P.text(tok, self.BODY)
        self.assertIn("hang up", said)
        [o] = self.texts()
        self.assertEqual(o["args"], {"find": "f_x", "pick": "p_a", "body": self.BODY})   # only in the open order
        self.assertIn("already been used", P.text(tok, self.BODY))
        self.assertEqual(len(self.texts()), 1)
        ev = next(e for e in P.read_log(50) if e["ev"] == "text")
        self.assertEqual((ev["name"], ev["last3"], ev["len"], ev["parts"]), ("Lena Brennan", "452", len(self.BODY), 1))
        tok_ev = next(e for e in P.read_log(50) if e["ev"] == "token")
        self.assertEqual(tok_ev["len"], len(self.BODY))
        t = json.loads((P.DIR / "tokens.json").read_text())["tokens"][0]
        self.assertEqual(t["body_sha256"], P._sha(self.BODY))

    def test_wrong_used_expired_and_call_tokens_refused(self):
        self.find_text()
        self.assertIn("doesn't match", P.text("zzzzzz", self.BODY))
        self.assertEqual(P.read_log(1)[0]["why"], "wrong")
        [call_tok] = self.tokens(self.find([self.LENA]))                     # a call token is not a text token
        self.assertIn("was for a call, not a text", P.text(call_tok, self.BODY))
        self.assertEqual(P.read_log(1)[0]["why"], "action")
        P.TOKEN_LIFE_S = 0.2
        [tok] = self.tokens(self.find_text())
        time.sleep(0.3)
        self.assertIn("run out", P.text(tok, self.BODY))
        self.assertEqual(P.read_log(1)[0]["why"], "expired")
        self.assertEqual(self.texts(), [])

    def test_a_changed_body_is_refused_and_the_read_back_one_still_goes(self):
        [tok] = self.tokens(self.find_text())
        for changed in ("Running late, gate code 482914 at 5:30pm", self.BODY + ".", self.BODY.lower()):
            self.assertIn("changed since I read it back", P.text(tok, changed))
            self.assertEqual(P.read_log(1)[0]["why"], "body_changed")
        self.assertEqual(self.texts(), [])
        self.assertIn("hang up", P.text(tok, f"  {self.BODY} "))         # spaces round the edges don't count
        self.assertEqual(self.texts()[0]["args"]["body"], self.BODY)

    def test_body_rules_empty_long_control_and_newline_kept(self):
        for bad, words in (("", "no text"), ("   ", "no text"), ("x" * 301, "over 300"),
                           ("hi\x07there", "control"), ("hi\u2028there", "control"), ("a\tb", "control")):
            self.assertIn(words, P.find_contact("Lena", "text", bad))         # refused before asking the phone
            self.assertIn(words, P.text("abcdef", bad))
            self.assertEqual(P.read_log(1)[0]["ev"], "token_refused")
        self.assertEqual(list((P.DIR / "orders").glob("*.json")), [])
        two_lines = "Running late\nsee you at 6"
        [tok] = self.tokens(self.find_text(body=two_lines))
        self.assertIn("hang up", P.text(tok, two_lines))
        self.assertEqual(self.texts()[0]["args"]["body"], two_lines)
        self.assertEqual(P.body_problem("x" * 300), "")                    # 300 exactly is fine

    def test_rate_cap_and_one_pending_are_shared_with_calls(self):
        [tok] = self.tokens(self.find([self.LENA]))
        P.call(tok)                                                        # a call on its way blocks a text
        [tok] = self.tokens(self.find_text())
        self.assertIn("already on its way", P.text(tok, self.BODY))
        self.assertEqual(P.read_log(1)[0]["why"], "pending")
        self.take_texts()
        [tok] = self.tokens(self.find_text())                               # and a text blocks a call
        said = P.text(tok, self.BODY)
        self.assertIn("hang up", said)
        self.assertIn("end it on their phone himself", said)
        [tok] = self.tokens(self.find([self.LENNY], name="Lenny"))
        self.assertIn("already on its way", P.call(tok))
        self.take_texts()
        [tok] = self.tokens(self.find_text())                               # 3rd in 10 min (call, text, text)
        self.assertIn("hang up", P.text(tok, self.BODY))
        self.take_texts()
        for _ in range(7):                                                  # 4th to 10th
            [tok] = self.tokens(self.find_text())
            self.assertIn("hang up", P.text(tok, self.BODY))
            self.take_texts()
        out = self.find_text()
        self.assertIn("most I'm allowed", out)
        self.assertEqual(self.tokens(out), [])
        self.assertIn("most I'm allowed", self.find([self.LENA]))

    def test_digit_check_skips_the_body_only(self):
        o = P.send("text", {"find": "f_x", "pick": "p_a", "body": "call 0412 345 678"}, device=self.device)
        self.assertEqual(o["args"]["body"], "call 0412 345 678")
        for k in ("pick", "find", "name"):
            with self.assertRaises(ValueError):
                P.send("text", {"find": "f_x", "pick": "p_a", "body": "hi", k: "0412345678"}, device=self.device)
        with self.assertRaises(ValueError):
            P.send("text", {"find": "f_x", "pick": "p_a", "bodyx": "123456"}, device=self.device)

    def test_body_scrubbed_on_delivery(self):
        [tok] = self.tokens(self.find_text())
        P.text(tok, self.BODY)
        o = P.next_order(self.device)                                      # the phone gets the words ...
        self.assertEqual(o["args"]["body"], self.BODY)
        [stored] = self.texts()                                             # ... the store keeps only the length
        self.assertEqual(stored["args"], {"find": "f_x", "pick": "p_a", "body_len": len(self.BODY)})
        code, why = P.answer(self.device, {"v": 1, "id": o["id"], "ok": True, "outcome": "sent",
                                           "data": {"after_s": 2.6, "parts": 1, "how": "sms"}})
        self.assertEqual(code, 200, why)
        ev = P.read_log(1)[0]
        self.assertEqual((ev["ev"], ev["len"], ev["parts"], ev["how"]), ("sent", len(self.BODY), 1, "sms"))
        [line] = P.call_start_lines()                                       # sent: said once, with the time
        self.assertIn("Your text to Lena Brennan (mobile ending 4-5-2) sent at ", line)
        self.assertNotIn(self.BODY, line)
        self.assertEqual(P.call_start_lines(), [])
        self.assert_no_body_on_disk()

    def test_body_scrubbed_on_expiry_delivered_or_not(self):
        [tok] = self.tokens(self.find_text())
        P.text(tok, self.BODY)
        [o] = self.texts()
        o["expires"] = time.time() - 1                                      # the phone never asked in time
        (P.DIR / "orders" / f"{o['id']}.json").write_text(json.dumps(o))
        self.assertEqual(P.scrub_stale(), 1)
        [o] = self.texts()
        self.assertEqual((o["state"], o["args"].get("body_len")), ("expired", len(self.BODY)))
        [line] = P.call_start_lines()
        self.assertIn("I'm not sure the phone texted Lena Brennan", line)
        self.assertIn("Messages", line)
        self.assert_no_body_on_disk()
        for f in (P.DIR / "orders").glob("*.json"):                         # each path that expires scrubs
            f.unlink()
        for path in ("next_order", "wait_result", "outcomes", "prune"):
            P.DIR.joinpath("tokens.json").unlink()
            [tok] = self.tokens(self.find_text())
            P.text(tok, self.BODY)
            [o] = self.texts()
            o["expires"] = time.time() - 1
            (P.DIR / "orders" / f"{o['id']}.json").write_text(json.dumps(o))
            {"next_order": lambda: P.next_order(self.device), "wait_result": lambda: P.wait_result(o["id"], 0),
             "outcomes": lambda: P.outcomes(mark=False), "prune": P.prune}[path]()
            [o] = self.texts()
            self.assertNotIn("body", o["args"], path)
            self.assert_no_body_on_disk()
            o["state"] = "answered"
            (P.DIR / "orders" / f"{o['id']}.json").write_text(json.dumps(o))
            for f in (P.DIR / "orders").glob("*.json"):
                f.unlink()

    def test_tool_text_for_route_a_route_b_and_not_known(self):
        out = self.find_text()
        self.assertIn("if the phone shows a 'Tap to text' notification instead", out)   # no ping yet
        self.assertIn("or, if the phone shows a 'Tap to text' notification", P.text(self.tokens(out)[0], self.BODY))
        self.take_texts()
        self.route(True)
        self.assertTrue(P.devices()[self.device]["send_sms"])
        out = self.find_text()
        self.assertIn("Then tell them: hang up and I'll send it in a few seconds.", out)
        said = P.text(self.tokens(out)[0], self.BODY)
        self.assertIn("it sends a few seconds after this call ends", said)
        self.assertNotIn("Tap to text", said)
        self.take_texts()
        self.route(False)
        out = self.find_text()
        self.assertIn("tap the 'Tap to text' notification on the phone and press Send", out)
        said = P.text(self.tokens(out)[0], self.BODY)
        self.assertIn("tap the 'Tap to text' notification on the phone and press Send", said)

    def take_texts(self):
        """The fake phone answers every call or text on its way, including one a find's fake phone already took."""
        while P.next_order(self.device):
            pass
        for o in self.calls() + self.texts():
            if o["state"] == "delivered":
                P.answer(self.device, {"v": 1, "id": o["id"], "ok": True,
                                       "outcome": "sent" if o["kind"] == "text" else "dialled", "data": {}})

    def test_route_b_done_closes_the_watch_quietly_and_is_logged_under_text(self):
        [tok] = self.tokens(self.find_text())
        P.text(tok, self.BODY)
        o = P.next_order(self.device)
        self.answer(o, "done", ok=True, data={"after_s": 2.5, "parts": 1, "how": "messages_app"})
        ev = P.read_log(1)[0]
        self.assertEqual((ev["ev"], ev["how"], ev["outcome"], ev["len"]), ("text", "messages_app", "done", len(self.BODY)))
        [line] = P.call_start_lines()                                       # said once: he has to press Send
        self.assertIn("'Tap to text' notification at ", line)
        self.assertIn("only went if he pressed Send", line)
        self.assertEqual(P.call_start_lines(), [])
        self.assertTrue(all(w["told"] for w in P._watched()))

    def test_text_not_sent_is_said_once_with_text_words(self):
        [tok] = self.tokens(self.find_text())
        P.text(tok, self.BODY)
        self.answer(P.next_order(self.device), "cancelled", error="the Claude call did not end within a minute")
        [line] = P.call_start_lines()
        self.assertIn("I didn't text Lena Brennan (mobile ending 4-5-2)", line)
        self.assertEqual(P.call_start_lines(), [])
        [tok] = self.tokens(self.find_text())
        P.text(tok, self.BODY)
        self.answer(P.next_order(self.device), "refused", error="that is a landline, so it can't take a text")
        [r] = P.outcomes()
        self.assertIn("refused to text Lena Brennan (mobile ending 4-5-2): that is a landline", r)
        self.assertIn("Nothing was sent.", r)

    def test_not_textable_contact_gets_no_token(self):
        home = {**self.LENNY, "callable": False, "why_not": "that is a landline, so it can't take a text"}
        out = self.find_text([home], name="Lenny")
        self.assertEqual(self.tokens(out), [])
        self.assertIn("can't text, that is a landline", out)
        self.assertIn("not a number I'm allowed to text", out)

    def test_sms_parts(self):
        self.assertEqual(P.sms_parts("x" * 160), 1)
        self.assertEqual(P.sms_parts("x" * 161), 2)
        self.assertEqual(P.sms_parts("x" * 300), 2)
        self.assertEqual(P.sms_parts("€" * 80), 1)                         # two septets each: 160
        self.assertEqual(P.sms_parts("€" * 81), 2)
        self.assertEqual(P.sms_parts("ok 👍"), 1)
        self.assertEqual(P.sms_parts("👍" * 36), 2)                         # 72 UTF-16 units
        self.assertEqual(P.sms_parts("ā" * 300), 5)


class Cli(Base):
    def setUp(self):
        super().setUp()
        self.httpd = A.serve(port=0, block=False)
        self.httpd.daemon_threads = False          # server_close() then waits for every handler thread
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        super().tearDown()

    def test_send_wait_gets_the_phone_answer_and_exit_codes(self):
        device, key = self.paired()

        def fake_phone(outcome="done", ok=True):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            h = {"Tailscale-User-Login": LOGIN, "X-Phone-Device": device, "Authorization": f"Bearer {key}"}
            c.request("GET", "/phone/api/next?wait=5", headers=h)
            o = json.loads(c.getresponse().read())
            c.close()
            body = json.dumps({"id": o["id"], "ok": ok, "outcome": outcome, "data": {"version": "0.1.0"}}).encode()
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("POST", "/phone/api/result", body=body, headers={**h, "Content-Length": str(len(body))})
            c.getresponse().read()
            c.close()

        t = threading.Thread(target=fake_phone); t.start()
        self.assertEqual(P.main(["send", "ping", "--wait"]), 0)
        t.join(5)
        t = threading.Thread(target=fake_phone, kwargs={"outcome": "unsupported", "ok": False}); t.start()
        self.assertEqual(P.main(["send", "dance", "--wait"]), 1)
        t.join(5)
        self.assertEqual(P.main(["send", "ping", "--wait", "--timeout", "0.3"]), 2)   # nobody answers
        self.assertEqual(P.main(["send", "find", "--name", "Lena", "--action", "call"]), 0)
        self.assertEqual(P.next_order(device)["kind"], "ping")   # the unanswered ping is still live, oldest first
        o = P.next_order(device)
        self.assertEqual(o["args"], {"name": "Lena", "action": "call"})

    def test_send_without_a_paired_phone(self):
        self.assertEqual(P.main(["send", "ping"]), 2)

    def test_pair_devices_revoke(self):
        self.assertEqual(P.main(["pair", "--name", "my-phone"]), 0)
        self.assertEqual(P.main(["devices"]), 0)
        device, _ = self.paired()
        self.assertEqual(P.main(["revoke", device]), 0)
        self.assertEqual(P.main(["revoke", "d_nobody"]), 1)


class TestInbox(Base):
    """Incoming texts and missed calls (4 Nov: the phone posts them, call_start says them
    once, phone_inbox reads the texts and marks them heard. The words never reach the log; who is never a number."""

    def post(self, device, key, items):
        body = json.dumps({"items": items}).encode()
        h = {"Tailscale-User-Login": LOGIN, "Authorization": f"Bearer {key}", "X-Phone-Device": device,
             "Content-Length": str(len(body))}
        code, out = P.handle("POST", "/phone/api/inbox", h, lambda n: body, want_login=LOGIN)
        return code, json.loads(out or b"{}")

    def test_post_needs_the_device_key(self):
        device, key = self.paired()
        self.assertEqual(self.post(device, "wrong", [])[0], 401)
        self.assertEqual(self.post(device, key, []), (200, {"ok": True, "new": 0}))

    def test_texts_and_missed_calls_kept_once_and_said_once_then_read_and_heard(self):
        device, key = self.paired()
        now = time.time()
        items = [{"kind": "missed", "at": now - 120, "who": "Lena", "known": True, "last3": "421"},
                 {"kind": "sms", "at": now - 60, "who": "Lena", "known": True, "last3": "421",
                  "body": "Running late, ring me on 0412 345 678"},
                 {"kind": "sms", "at": now - 30, "who": "", "known": False, "last3": "987", "body": "Hi it's Bob"}]
        self.assertEqual(self.post(device, key, items), (200, {"ok": True, "new": 3}))
        self.assertEqual(self.post(device, key, items), (200, {"ok": True, "new": 0}))     # a resend is not new
        line = " ".join(P.inbox_call_start_lines())
        self.assertIn("Lena tried to call at", line)
        self.assertIn("2 new texts from Lena, an unknown number ending 9-8-7", line)
        self.assertNotIn("Running late", line)                                     # the words wait for phone_inbox
        self.assertEqual(P.inbox_call_start_lines(), [])                           # said once
        out = P.inbox()
        self.assertIn('Text from Lena at', out)
        self.assertIn("Running late, ring me on 0412 345 678", out)                # their texts' words, digits and all
        self.assertIn("an unknown number ending 9-8-7", out)
        self.assertNotIn("Missed call", out)                                       # call_start already said it
        self.assertIn("No new texts or missed calls", P.inbox())                   # heard
        self.assertIn("Running late", P.inbox(recent=True))
        log = (P.DIR / "phone.log").read_text()
        self.assertNotIn("Running late", log)
        self.assertNotIn("345", log)
        self.assertIn('"inbox"', log)
        self.assertEqual(stat.S_IMODE(os.stat(P.DIR / "inbox.json").st_mode), 0o600)

    def test_who_never_a_number_and_bad_shapes_refused(self):
        device, key = self.paired()
        now = time.time()
        bad = [{"kind": "sms", "at": now, "who": "0412 345 678", "known": False, "last3": "678", "body": "x"},
               {"kind": "sms", "at": now, "who": "", "known": False, "last3": "5678", "body": "x"},
               {"kind": "missed", "at": now, "who": "", "known": False, "last3": "", "body": "x"},
               {"kind": "sms", "at": now, "who": "", "known": False, "last3": ""},
               {"kind": "mms", "at": now, "who": "", "known": False, "last3": ""},
               {"kind": "missed", "at": now - 30 * 86400, "who": "", "known": False, "last3": ""},
               {"kind": "missed", "at": now, "who": "", "known": False, "last3": "", "number": "x"}]
        for it in bad:
            self.assertEqual(self.post(device, key, [it])[0], 400, it)
        self.assertEqual(self.post(device, key, [{"kind": "missed", "at": now}] * 51)[0], 400)
        self.assertFalse((P.DIR / "inbox.json").exists())

    def test_one_time_code_held_back_unless_asked(self):
        device, key = self.paired()
        self.post(device, key, [{"kind": "sms", "at": time.time(), "who": "Westpac", "known": False, "last3": "",
                                 "body": "Your Westpac verification code is 482913. Never share it."}])
        out = P.inbox(mark=False)
        self.assertIn("Westpac (not a saved contact)", out)
        self.assertIn("one-time security code", out)
        self.assertNotIn("482913", out)
        self.assertIn("482913", P.inbox(show_codes=True))
        self.assertFalse(P.looks_like_code("See you at 1530 at the shed"))

    def test_private_number_and_old_items_dropped_after_a_week(self):
        device, key = self.paired()
        now = time.time()
        self.post(device, key, [{"kind": "missed", "at": now - 10, "who": "", "known": False, "last3": ""}])
        self.assertIn("A private number tried to call", " ".join(P.inbox_call_start_lines()))
        items = json.loads((P.DIR / "inbox.json").read_text())["items"]
        items[0]["at"] = now - 8 * 86400
        (P.DIR / "inbox.json").write_text(json.dumps({"items": items}))
        P.inbox()
        self.assertEqual(json.loads((P.DIR / "inbox.json").read_text())["items"], [])

    def test_says_what_the_phone_may_not_read(self):
        device, key = self.paired()
        def nxt(reads=None, ver="0.2.3"):
            h = {"Tailscale-User-Login": LOGIN, "Authorization": f"Bearer {key}", "X-Phone-Device": device,
                 "X-App-Version": ver}
            if reads is not None:
                h["X-Phone-Reads"] = reads
            P.handle("GET", "/phone/api/next?wait=0", h, lambda n: b"", want_login=LOGIN)
        nxt(ver="0.2.2")                                                   # no header: an older app
        self.assertIn("older than 0.2.3", P.inbox())
        nxt("none")
        self.assertIn("isn't allowed to read texts or missed calls yet", P.inbox())
        self.assertIn("Allow texts and calls", P.inbox())
        nxt("sms")
        self.assertIn("read missed calls yet", P.inbox())
        nxt("0412345678")                                                  # not a value it knows: kept as it was
        self.assertEqual(P.devices()[device]["reads"], "sms")
        self.assertIn("Allow texts and calls", " ".join(P.inbox_call_start_lines()))   # call_start: once
        self.assertEqual(P.inbox_call_start_lines(), [])
        nxt("sms,calls")
        self.assertEqual(P.inbox(), "No new texts or missed calls.")
        self.assertEqual(P.inbox_call_start_lines(), [])


if __name__ == "__main__":
    unittest.main()
