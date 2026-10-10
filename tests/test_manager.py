import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from claude_keyproxy import manager
from claude_keyproxy import server as keyproxy

NOWHERE = "http://127.0.0.1:1"  # upstream for these tests: nothing here should ever be forwarded

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

FAKE_CLAUDE = """#!{python}
import sys
print("\\x1b]8;id=1;https://claude.example/oauth/authorize?state=abc\\x1b\\\\sign in\\x1b]8;;\\x1b\\\\", flush=True)
print("Paste code here if prompted >", flush=True)
code = sys.stdin.readline().strip()
if code == "good-code-123":
    print("Your token:", flush=True)
    print("sk-ant-oat01-" + "x" * 40, flush=True)
else:
    print("OAuth error: invalid code", flush=True)
    sys.exit(1)
"""


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "keys"))
        self.work = os.path.join(self.tmp.name, "keys", "work.key")
        with open(self.work, "w") as f:
            f.write("token-work-1111\n")
        self.index = os.path.join(self.tmp.name, "index.json")
        with open(self.index, "w") as f:
            json.dump({"keys": [{"priority": 1, "name": "work", "path": self.work}]}, f)
        self.server = keyproxy.make_server(0, self.index, NOWHERE)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        h = {"content-type": "application/json"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with opener.open(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_serves_the_page_and_its_assets(self):
        for path, ctype in [("/", "text/html"), ("/_keyproxy/pico.min.css", "text/css"),
                            ("/_keyproxy/style.css", "text/css"), ("/_keyproxy/app.js", "text/javascript"),
                            ("/_keyproxy/alpine.min.js", "text/javascript")]:
            with opener.open(self.base + path) as resp:
                self.assertEqual(resp.status, 200, path)
                self.assertTrue(resp.headers["content-type"].startswith(ctype), path)
                self.assertTrue(resp.read(), path)

    def fake_claude(self):
        path = os.path.join(self.tmp.name, "claude")
        with open(path, "w") as f:
            f.write(FAKE_CLAUDE.format(python=sys.executable))
        os.chmod(path, 0o755)
        os.environ["KEYPROXY_CLAUDE"] = path
        self.addCleanup(os.environ.pop, "KEYPROXY_CLAUDE", None)

    def test_sign_in_adds_a_key(self):
        self.fake_claude()
        status, start = self.call("POST", "/_keyproxy/api/setup/start", {"name": "personal", "replace": False})
        self.assertEqual(status, 200)
        self.assertEqual(start["url"], "https://claude.example/oauth/authorize?state=abc")
        status, keys = self.call("POST", "/_keyproxy/api/setup/finish", {"id": start["id"], "code": "good-code-123"})
        self.assertEqual(status, 200)
        self.assertEqual([(k["name"], k["last4"]) for k in keys], [("work", "1111"), ("personal", "xxxx")])
        with open(os.path.join(self.tmp.name, "keys", "personal.key")) as f:
            self.assertEqual(f.read(), "sk-ant-oat01-" + "x" * 40 + "\n")
        self.assertNotIn("sk-ant-oat01", json.dumps(keys))

    def test_sign_in_replaces_a_token(self):
        self.fake_claude()
        status, start = self.call("POST", "/_keyproxy/api/setup/start", {"name": "work", "replace": True})
        self.assertEqual(status, 200)
        status, keys = self.call("POST", "/_keyproxy/api/setup/finish", {"id": start["id"], "code": "good-code-123"})
        self.assertEqual(status, 200)
        self.assertEqual(keys[0]["last4"], "xxxx")

    def test_sign_in_with_a_bad_code_changes_nothing(self):
        self.fake_claude()
        _, start = self.call("POST", "/_keyproxy/api/setup/start", {"name": "personal", "replace": False})
        status, body = self.call("POST", "/_keyproxy/api/setup/finish", {"id": start["id"], "code": "wrong-code-1"})
        self.assertEqual(status, 400)
        self.assertIn("invalid code", body["error"])
        self.assertEqual([k["name"] for k in self.call("GET", "/_keyproxy/api/keys")[1]], ["work"])
        status, _ = self.call("POST", "/_keyproxy/api/setup/finish", {"id": start["id"], "code": "good-code-123"})
        self.assertEqual(status, 400)  # the sign-in is gone after one try

    def test_sign_in_checks_the_name_before_running_claude(self):
        self.fake_claude()
        for body in [{"name": "work", "replace": False}, {"name": "nope", "replace": True}, {"name": "a b"}]:
            status, _ = self.call("POST", "/_keyproxy/api/setup/start", body)
            self.assertIn(status, (400, 404), body)

    def test_cancel_stops_the_sign_in(self):
        self.fake_claude()
        _, start = self.call("POST", "/_keyproxy/api/setup/start", {"name": "personal", "replace": False})
        self.assertEqual(self.call("POST", "/_keyproxy/api/setup/cancel", {"id": start["id"]})[0], 200)
        status, _ = self.call("POST", "/_keyproxy/api/setup/finish", {"id": start["id"], "code": "good-code-123"})
        self.assertEqual(status, 400)

    def test_sign_in_without_claude(self):
        os.environ["KEYPROXY_CLAUDE"] = ""
        self.addCleanup(os.environ.pop, "KEYPROXY_CLAUDE", None)
        old = os.environ.get("PATH")
        os.environ["PATH"] = ""
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        status, body = self.call("POST", "/_keyproxy/api/setup/start", {"name": "personal", "replace": False})
        self.assertEqual(status, 400)
        self.assertIn("not found", body["error"])

    def test_sign_in_with_a_fake_session(self):
        """No process at all: Signins takes the session from a factory."""
        class FakeSession:
            def __init__(self, claude):
                self.claude, self.closed = claude, False

            def url(self):
                return "https://claude.example/oauth/authorize?x"

            def token(self, code):
                assert code == "good-code-123"  # stripped before it reaches the session
                return "sk-ant-oat01-" + "y" * 40

            def close(self):
                self.closed = True

        signins = manager.Signins(manager.Keys(self.index), open_session=FakeSession, claude=lambda: "/bin/claude")
        start = signins.start("personal", False)
        self.assertEqual(start["url"], "https://claude.example/oauth/authorize?x")
        signins.finish(start["id"], "  good-code-123  ")
        self.assertEqual([k["last4"] for k in signins.keys.all()], ["1111", "yyyy"])

    def test_add_replace_and_reorder(self):
        status, keys = self.call("GET", "/_keyproxy/api/keys")
        self.assertEqual(keys, [{"name": "work", "priority": 1, "last4": "1111", "disabled": False,
                                 "api_key": False}])
        self.assertNotIn("token-work", json.dumps(keys))

        status, keys = self.call("POST", "/_keyproxy/api/keys", {"name": "personal", "token": "token-personal-2222"})
        self.assertEqual(status, 201)
        self.assertEqual([(k["name"], k["priority"], k["last4"]) for k in keys],
                         [("work", 1, "1111"), ("personal", 2, "2222")])
        added = os.path.join(self.tmp.name, "keys", "personal.key")
        self.assertEqual(stat.S_IMODE(os.stat(added).st_mode), 0o600)

        status, keys = self.call("PUT", "/_keyproxy/api/keys/work", {"token": "token-work-3333"})
        self.assertEqual(status, 200)
        with open(self.work) as f:
            self.assertEqual(f.read(), "token-work-3333\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.work).st_mode), 0o600)

        status, keys = self.call("PUT", "/_keyproxy/api/order", {"order": ["personal", "work"]})
        self.assertEqual([k["name"] for k in keys], ["personal", "work"])
        with open(self.index) as f:
            saved = {k["name"]: k for k in json.load(f)["keys"]}
        self.assertEqual(saved["work"], {"priority": 2, "name": "work", "path": self.work})

    def test_first_run_without_an_index(self):
        index = os.path.join(self.tmp.name, "fresh", "index.json")
        server = keyproxy.make_server(0, index, NOWHERE)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.assertEqual(self.call("GET", "/_keyproxy/api/keys"), (200, []))
        status, keys = self.call("POST", "/_keyproxy/api/keys", {"name": "work", "token": "token-new-2222"})
        self.assertEqual((status, keys[0]["last4"]), (201, "2222"))
        self.assertEqual(stat.S_IMODE(os.stat(index).st_mode), 0o600)
        with open(index) as f:
            self.assertEqual(json.load(f)["keys"][0]["name"], "work")

    def test_open_shows_the_token_file_without_returning_it(self):
        opened = []
        self.server.keys.open_file = opened.append
        self.assertEqual(self.call("POST", "/_keyproxy/api/keys/work/open", {}), (200, {}))
        self.assertEqual(opened, [self.work])
        self.assertEqual(self.call("POST", "/_keyproxy/api/keys/nope/open", {})[0], 404)
        os.unlink(self.work)
        self.assertEqual(self.call("POST", "/_keyproxy/api/keys/work/open", {})[0], 404)

    def test_delete(self):
        self.call("POST", "/_keyproxy/api/keys", {"name": "personal", "token": "token-personal-2222"})
        evil = {"origin": "https://evil.example"}
        self.assertEqual(self.call("DELETE", "/_keyproxy/api/keys/work", headers=evil)[0], 403)
        status, keys = self.call("DELETE", "/_keyproxy/api/keys/work")
        self.assertEqual((status, keys), (200, [{"name": "personal", "priority": 1, "last4": "2222",
                                                 "disabled": False, "api_key": False}]))
        self.assertFalse(os.path.exists(self.work))
        self.assertEqual(self.call("DELETE", "/_keyproxy/api/keys/work")[0], 404)

    def test_disable_skips_the_key_until_it_is_turned_back_on(self):
        self.assertEqual([k.name for k in self.server.pool.load()], ["work"])
        status, keys = self.call("PUT", "/_keyproxy/api/keys/work/disabled", {"disabled": True})
        self.assertEqual((status, keys[0]["disabled"]), (200, True))
        self.assertEqual(self.server.pool.load(), [])
        status, keys = self.call("PUT", "/_keyproxy/api/keys/work/disabled", {"disabled": False})
        self.assertEqual((status, keys[0]["disabled"]), (200, False))
        self.assertEqual([k.name for k in self.server.pool.load()], ["work"])
        with open(self.index) as f:
            self.assertNotIn("disabled", json.load(f)["keys"][0])
        self.assertEqual(self.call("PUT", "/_keyproxy/api/keys/nope/disabled", {"disabled": True})[0], 404)
        self.assertEqual(self.call("PUT", "/_keyproxy/api/keys/work/disabled", {"disabled": "yes"})[0], 400)

    def test_rejects_bad_input(self):
        self.assertEqual(self.call("POST", "/_keyproxy/api/keys", {"name": "../x", "token": "token-abcdefgh"})[0], 400)
        self.assertEqual(self.call("POST", "/_keyproxy/api/keys", {"name": "work", "token": "token-abcdefgh"})[0], 400)
        self.assertEqual(self.call("PUT", "/_keyproxy/api/keys/nope", {"token": "token-abcdefgh"})[0], 404)
        self.assertEqual(self.call("PUT", "/_keyproxy/api/order", {"order": ["work", "work"]})[0], 400)
        self.assertEqual(self.call("PUT", "/_keyproxy/api/keys/work", {"token": "short"})[0], 400)

    def test_rejects_other_sites_and_non_json(self):
        evil = {"origin": "https://evil.example"}
        self.assertEqual(self.call("PUT", "/_keyproxy/api/keys/work", {"token": "token-abcdefgh"}, evil)[0], 403)
        self.assertEqual(self.call("GET", "/_keyproxy/api/keys", headers={"host": "evil.example"})[0], 403)
        req = urllib.request.Request(self.base + "/_keyproxy/api/keys/work", method="PUT", data=b"token=x",
                                     headers={"content-type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(req)
        self.assertEqual(ctx.exception.code, 415)
        with open(self.work) as f:
            self.assertEqual(f.read(), "token-work-1111\n")

    def test_cache_show_and_reset(self):
        self.server.pool.cooldowns.until["work"] = time.time() + 120
        status, cache = self.call("GET", "/_keyproxy/api/cache")
        self.assertEqual((status, list(cache)), (200, ["work"]))
        self.assertEqual(self.call("POST", "/_keyproxy/api/cache/reset", {},
                                   {"origin": "https://evil.example"})[0], 403)
        self.assertEqual(list(self.call("GET", "/_keyproxy/api/cache")[1]), ["work"])
        self.server.pool.usage["work"] = {"status": "rejected", "5h": None, "7d": None, "seen": 0}
        self.assertEqual(self.call("POST", "/_keyproxy/api/cache/reset", {}), (200, {}))
        self.assertEqual(self.call("GET", "/_keyproxy/api/usage")[1], {"work": None})

    def test_headless_keeps_only_the_read_only_calls(self):
        self.server.headless = True
        self.assertEqual(self.call("GET", "/_keyproxy/api/keys")[0], 200)
        self.assertEqual(self.call("GET", "/_keyproxy/api/cache")[0], 200)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(self.base + "/")
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(self.call("POST", "/_keyproxy/api/keys", {"name": "x", "token": "token-abcdefgh"})[0], 404)
        self.assertEqual(self.call("DELETE", "/_keyproxy/api/keys/work")[0], 404)
        self.assertTrue(os.path.exists(self.work))

    def test_manager_paths_are_never_forwarded(self):
        self.assertEqual(self.call("GET", "/_keyproxy/nope")[0], 404)
        self.assertEqual(self.call("POST", "/_keyproxy/api/nope", {})[0], 404)

    def test_merge_usage(self):
        keys = [{"name": "work", "api_key": False}, {"name": "spare", "api_key": False},
                {"name": "billing", "api_key": True}]
        seen = {"work": {"status": "allowed", "5h": {"utilization": 0.4, "reset": 1}, "7d": None},
                "gone": {"status": "allowed"}}
        self.assertEqual(manager.merge_usage(keys, seen),
                         {"work": seen["work"], "spare": None, "billing": "n/a"})


if __name__ == "__main__":
    unittest.main()
