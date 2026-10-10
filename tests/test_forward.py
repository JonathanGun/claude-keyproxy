import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from claude_keyproxy import server as keyproxy

# urllib would otherwise send requests through the macOS system proxy
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FakeAnthropic:
    """Upstream stand-in. Tokens in `limited` get a 429, the others get a 200 stream."""

    def __init__(self):
        self.limited = set()
        self.seen = []  # (token, anthropic-beta) per request
        self.api_keys = []  # x-api-key per request
        self.bodies = []  # request body per request
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                outer.bodies.append(self.rfile.read(int(self.headers.get("content-length") or 0)))
                token = (self.headers["authorization"] or "").removeprefix("Bearer ")
                outer.seen.append((token, self.headers.get("anthropic-beta")))
                outer.api_keys.append(self.headers.get("x-api-key"))
                if token in outer.limited:
                    self.send_response(429)
                    self.send_header("anthropic-ratelimit-unified-reset", str(int(time.time()) + 60))
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("anthropic-ratelimit-unified-status", "allowed")
                self.send_header("anthropic-ratelimit-unified-5h-utilization", "0.42")
                self.send_header("anthropic-ratelimit-unified-5h-reset", "1760000000")
                self.send_header("anthropic-ratelimit-unified-7d-utilization", "0.9")
                self.send_header("anthropic-ratelimit-unified-7d-reset", "1760500000")
                self.end_headers()
                for part in (b"data: one\n\n", b"data: two\n\n"):
                    self.wfile.write(part)
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class ForwardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        keys = []
        for priority, name in ((1, "first"), (2, "second")):
            path = os.path.join(self.tmp.name, f"{name}.key")
            with open(path, "w") as f:
                f.write(f"token-{name}\n")
            keys.append({"priority": priority, "name": name, "path": path})
        index = os.path.join(self.tmp.name, "index.json")
        with open(index, "w") as f:
            json.dump({"keys": keys}, f)

        self.upstream = FakeAnthropic()
        self.proxy = keyproxy.make_server(0, index, self.upstream.url)
        self.base = f"http://127.0.0.1:{self.proxy.server_port}"
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown()
        self.upstream.server.shutdown()
        self.tmp.cleanup()

    def post(self):
        req = urllib.request.Request(f"{self.base}/v1/messages", data=b"{}", method="POST",
                                     headers={"authorization": "Bearer dummy"})
        return opener.open(req)

    def test_uses_highest_priority_key_and_replaces_dummy_token(self):
        with self.post() as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"data: one\n\ndata: two\n\n")
        self.assertEqual([t for t, _ in self.upstream.seen], ["token-first"])

    def test_forwards_a_chunked_body_whole(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port)
        conn.request("POST", "/v1/messages", body=iter([b'{"a":', b' 1}']), encode_chunked=True,
                     headers={"authorization": "Bearer dummy", "transfer-encoding": "chunked"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        self.assertEqual(self.upstream.bodies, [b'{"a": 1}'])

    def test_uses_the_strategy_it_is_given(self):
        def last_first(keys, cooldowns):
            return sorted(keys, key=lambda k: -k.priority)
        self.proxy.pool.strategy = last_first
        self.post().read()
        self.assertEqual([t for t, _ in self.upstream.seen], ["token-second"])

    def test_adds_oauth_beta_header(self):
        self.post().read()
        self.assertIn("oauth-2025-04-20", self.upstream.seen[0][1])

    def test_fails_over_and_skips_limited_key_afterwards(self):
        self.upstream.limited.add("token-first")
        self.assertEqual(self.post().status, 200)
        self.assertEqual([t for t, _ in self.upstream.seen], ["token-first", "token-second"])

        self.upstream.seen.clear()
        self.assertEqual(self.post().status, 200)
        self.assertEqual([t for t, _ in self.upstream.seen], ["token-second"])

        with opener.open(f"{self.base}/_keyproxy/api/cache") as resp:
            status = json.load(resp)
        self.assertIn("first", status)
        self.assertNotIn("second", status)

    def test_returns_the_429_when_every_key_is_limited(self):
        self.upstream.limited.update({"token-first", "token-second"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post()
        self.assertEqual(ctx.exception.code, 429)

    def test_api_key_traffic_is_forwarded_untouched(self):
        req = urllib.request.Request(f"{self.base}/v1/messages", data=b"{}", method="POST",
                                     headers={"x-api-key": "sk-ant-mine"})
        opener.open(req).read()
        self.assertEqual(self.upstream.api_keys, ["sk-ant-mine"])
        self.assertEqual(self.upstream.seen, [("", None)])

    def test_key_comes_back_after_its_cooldown(self):
        self.upstream.limited.add("token-first")
        self.post().read()
        self.proxy.pool.cooldowns.until["first"] = time.time() - 1
        self.upstream.limited.clear()
        self.upstream.seen.clear()
        self.post().read()
        self.assertEqual([t for t, _ in self.upstream.seen], ["token-first"])

    def test_records_usage_headers_per_key(self):
        self.post().read()
        with opener.open(f"{self.base}/_keyproxy/api/usage") as resp:
            usage = json.load(resp)
        self.assertEqual(usage["first"]["5h"], {"utilization": 0.42, "reset": 1760000000})
        self.assertEqual(usage["first"]["7d"], {"utilization": 0.9, "reset": 1760500000})
        self.assertEqual(usage["first"]["status"], "allowed")
        self.assertIsNone(usage["second"])  # has not served a request, and a GET never probes

    def probe(self):
        req = urllib.request.Request(f"{self.base}/_keyproxy/api/usage/probe", b"{}",
                                     {"content-type": "application/json"}, method="POST")
        with opener.open(req) as resp:
            return json.load(resp)

    def test_probe_sends_one_tiny_request_per_key(self):
        self.assertEqual(self.probe()["second"]["status"], "allowed")
        self.assertEqual(sorted(t for t, _ in self.upstream.seen), ["token-first", "token-second"])
        self.assertEqual({json.loads(b)["max_tokens"] for b in self.upstream.bodies}, {1})

    def test_a_limited_probe_cools_the_key_down(self):
        self.upstream.limited = {"token-first"}
        self.probe()
        with opener.open(f"{self.base}/_keyproxy/api/cache") as resp:
            self.assertEqual(list(json.load(resp)), ["first"])

if __name__ == "__main__":
    unittest.main()
