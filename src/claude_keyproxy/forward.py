"""The request handler: forwards each request upstream with a key from the pool, and moves on to
the next key when one is limited. The key manager's paths go to manager.answer() instead."""
from __future__ import annotations

import http.client
import json
import socketserver
import sys
import threading
import time
from http.client import HTTPResponse
from http.server import ThreadingHTTPServer
from urllib.parse import SplitResult

from . import manager
from .http_json import JsonHandler
from .keypool import Key, KeyPool

OAUTH_BETA = "oauth-2025-04-20"
# The smallest request that returns the usage headers: about 9 tokens. Anthropic has no free way to read usage.
PROBE = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 1,
                    "messages": [{"role": "user", "content": "."}]}).encode()

REQUEST_DROP = {"host", "connection", "keep-alive", "transfer-encoding", "content-length",
                "authorization", "x-api-key", "proxy-authorization", "te", "upgrade"}
RESPONSE_DROP = {"connection", "keep-alive", "transfer-encoding", "proxy-authenticate", "upgrade"}


def log(*parts: object) -> None:
    print(time.strftime("%H:%M:%S"), *parts, file=sys.stderr, flush=True)


class ProxyServer(ThreadingHTTPServer):
    """The server, with the state its handler and the key manager read. make_server() fills it in."""

    daemon_threads = True
    headless: bool
    pool: KeyPool
    keys: manager.Keys
    signins: manager.Signins
    target: SplitResult
    connect: type[http.client.HTTPConnection]

    def server_bind(self) -> None:
        """HTTPServer.server_bind() without its reverse DNS lookup of the host, which can hang for
        seconds on macOS. The name it looks up is never used."""
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.socket.getsockname()[:2]

    def probe_all(self) -> None:
        """One 1-token request with every subscription key, so each shows its current usage. Only on request:
        it starts the 5-hour window of an account that is not in one."""
        keys = [k for k in self.pool.load() if k.token and not k.token.startswith("sk-ant-api")]
        threads = [threading.Thread(target=self.probe, args=(key,)) for key in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def probe(self, key: Key) -> None:
        conn = self.connect(self.target.netloc, timeout=15)
        headers = {"Authorization": f"Bearer {key.token}", "anthropic-beta": OAUTH_BETA,
                   "anthropic-version": "2023-06-01", "content-type": "application/json"}
        try:
            conn.request("POST", "/v1/messages", PROBE, headers)
            resp = conn.getresponse()
            resp.read()
        except OSError as e:
            log(key.name, "probe failed:", e)
            return
        finally:
            conn.close()
        wait = self.pool.observe(key.name, resp)
        log(key.name, "probe", resp.status, f"skip {key.name} for {wait}s" if wait is not None else "")


class Handler(JsonHandler):
    server: ProxyServer

    server_version = "keyproxy"
    extra_headers = (("cache-control", "no-store"),)  # on the manager's answers; relay() sends upstream's own

    def read_body(self) -> bytes:
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            return self.read_chunked()
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def read_chunked(self) -> bytes:
        body = b""
        while size := int(self.rfile.readline().split(b";")[0].strip() or b"0", 16):
            body += self.rfile.read(size)
            self.rfile.readline()
        while self.rfile.readline().strip():
            continue  # skip the trailer headers after the last chunk
        return body

    def upstream_headers(self, token: str | None) -> dict[str, str]:
        if token is None:  # API-key traffic, forward as it is
            return {k: v for k, v in self.headers.items()
                    if k.lower() not in REQUEST_DROP or k.lower() == "x-api-key"}
        headers = {k: v for k, v in self.headers.items() if k.lower() not in REQUEST_DROP}
        headers["Authorization"] = f"Bearer {token}"
        beta = headers.get("anthropic-beta", "")
        if OAUTH_BETA not in beta:
            headers["anthropic-beta"] = f"{beta},{OAUTH_BETA}" if beta else OAUTH_BETA
        return headers

    def handle_any(self) -> None:
        if manager.owns(self.path):
            manager.answer(self)
            return
        body = self.read_body()
        if "x-api-key" in self.headers and "authorization" not in self.headers:
            keys = [Key("api-key", None, 0)]
        else:
            keys = self.server.pool.candidates()
        for i, key in enumerate(keys):
            if self.try_key(key, body, last=i == len(keys) - 1):
                return
        self.send_error(503, "no keys available")

    def try_key(self, key: Key, body: bytes, last: bool) -> bool:
        """Send the request with one key. False means: try the next key."""
        log(key.name, self.command, self.path, "start")
        conn = self.server.connect(self.server.target.netloc, timeout=600)
        try:
            conn.request(self.command, self.path, body, self.upstream_headers(key.token))
            resp = conn.getresponse()
        except OSError as e:
            conn.close()
            if not last:
                return False
            log(key.name, self.command, self.path, "502", e)
            self.send_error(502, f"upstream error: {e}")
            return True
        try:
            if key.token is not None and self.skip(key.name, resp, last):
                resp.read()
                return False
            log(key.name, self.command, self.path, resp.status)
            self.relay(resp)
            return True
        finally:
            conn.close()

    def skip(self, name: str, resp: HTTPResponse, last: bool) -> bool:
        """Record the key's usage, and cool it down if it is limited or rejected. True: try the next key."""
        wait = self.server.pool.observe(name, resp)
        if wait is None or last:
            return False
        log(name, self.command, self.path, resp.status, f"skip {name} for {wait}s, next key")
        return True

    def relay(self, resp: HTTPResponse) -> None:
        try:
            self.send_response_only(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in RESPONSE_DROP:
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            while chunk := resp.read1(65536):  # read1 returns as data arrives, so SSE streams live
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return  # the client went away mid-stream

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = handle_any
