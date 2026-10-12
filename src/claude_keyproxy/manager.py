"""The key manager: a web page and JSON API, served by the proxy itself under /_keyproxy/.

    keyproxy-up --open     # starts the proxy if it is down, then opens http://127.0.0.1:18787/

It edits the same index.json and key files the proxy reads, so a change applies on
the next request. Tokens go in, never out: the API only shows the last 4 characters.
Only requests from the page's own origin are answered.

GET    /_keyproxy/api/keys           keys by priority, with the last 4 characters of each token
POST   /_keyproxy/api/keys           {"name", "token"} adds a key at the lowest priority
PUT    /_keyproxy/api/keys/<name>    {"token"} replaces that key's token
DELETE /_keyproxy/api/keys/<name>    removes that key from the index and deletes its token file
POST   /_keyproxy/api/keys/<name>/open  opens the token file in the default text editor. The token itself
                                     never passes through the API.
PUT    /_keyproxy/api/keys/<name>/disabled  {"disabled": bool} turns the key off or back on. The proxy
                                     skips a disabled key until it is turned back on.
PUT    /_keyproxy/api/order          {"order": [names]} sets priority 1..n in that order
GET    /_keyproxy/api/cache          the cooldowns, {name: seconds left}
POST   /_keyproxy/api/cache/reset    clears the cooldowns and the usage seen
GET    /_keyproxy/api/usage          {name: usage}: 5h and 7d utilization and reset,
                                     null before the key served a request, "n/a" for API keys
POST   /_keyproxy/api/usage/probe    the same, after one 1-token request with every subscription key.
                                     It starts the 5-hour window of an account that is not in one.
POST   /_keyproxy/api/setup/start    {"name", "replace"} runs `claude setup-token` and returns
                                     {"id", "url"}, the sign-in link. replace=true is for an existing key.
POST   /_keyproxy/api/setup/finish   {"id", "code"} gives the code from the sign-in page to `claude`,
                                     saves the token it returns under that name, and returns the keys.
POST   /_keyproxy/api/setup/cancel   {"id"} stops a sign-in
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from . import setup_token
from .http_json import Router

if TYPE_CHECKING:  # forward.py imports this module
    from .forward import Handler, ProxyServer
    from .keypool import Usage

Json = dict[str, Any]
T = TypeVar("T")

PREFIX = "/_keyproxy/"  # every path the manager owns starts with this, plus the page at /
API = "/_keyproxy/api"
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
FILES = {  # URL path -> (file in ui/, content type)
    "/": ("index.html", "text/html; charset=utf-8"),
    "/_keyproxy/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/_keyproxy/pico.min.css": ("pico.min.css", "text/css; charset=utf-8"),
    "/_keyproxy/theme.css": ("theme.css", "text/css; charset=utf-8"),
    "/_keyproxy/style.css": ("style.css", "text/css; charset=utf-8"),
    "/_keyproxy/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/_keyproxy/alpine.min.js": ("alpine.min.js", "text/javascript; charset=utf-8"),
}
NAME = re.compile(r"[A-Za-z0-9._-]+")
TOKEN = re.compile(r"\S{8,4096}")
CODE = re.compile(r"\S{8,2048}")


class BadRequest(ValueError):
    """Answered with 400."""


class NotFound(Exception):
    """Answered with 404."""


ERRORS = ((NotFound, 404), (ValueError, 400))  # ValueError covers BadRequest and bad JSON


def valid_name(name: object) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise BadRequest("name may only use letters, digits, '.', '_' and '-'")
    return name


def valid_token(token: object) -> str:
    if not isinstance(token, str) or not TOKEN.fullmatch(token.strip()):
        raise BadRequest("token must be 8 or more characters with no spaces")
    return token.strip()


def write_atomic(path: str, data: str, mode: int) -> None:
    """Write to a temp file in the same folder, then rename, so a reader never sees half a file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def locked(method: Callable[..., T]) -> Callable[..., T]:
    """Run a Keys method while holding its lock."""
    def run(self: Keys, *args: Any) -> T:
        with self.lock:
            return method(self, *args)
    return run


def open_in_editor(path: str) -> None:
    """Open a file in the default text editor, without waiting for it."""
    if sys.platform == "darwin":
        argv = ["open", "-t", path]
    elif shutil.which("xdg-open"):
        argv = ["xdg-open", path]
    else:
        raise BadRequest(f"no way to open an editor here. The token is in {path}")
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


class Keys:
    """index.json and the token files. `open_file(path)` shows a token file, and is a fake in tests."""

    def __init__(self, index: str, open_file: Callable[[str], None] = open_in_editor) -> None:
        self.index = index
        self.open_file = open_file
        self.lock = threading.Lock()  # the server is threaded, so writes take turns

    def read(self) -> Json:
        try:
            with open(self.index) as f:
                data: Json = json.load(f)
                return data
        except FileNotFoundError:  # first run: the first save creates it
            return {"keys": []}

    def save(self, data: Json) -> None:
        try:
            mode = stat.S_IMODE(os.stat(self.index).st_mode)
        except FileNotFoundError:
            os.makedirs(os.path.dirname(self.index), mode=0o700, exist_ok=True)
            mode = 0o600
        write_atomic(self.index, json.dumps(data, indent=2) + "\n", mode)

    def all(self) -> list[Json]:
        """Every key by priority, as the API shows it: no token, only its last 4 characters."""
        out = []
        for k in sorted(self.read()["keys"], key=lambda k: k["priority"]):
            try:
                with open(os.path.expanduser(k["path"])) as f:
                    token = "".join(f.read().split())
            except OSError:
                token = None
            out.append({"name": k["name"], "priority": k["priority"],
                        "last4": token and token[-4:], "disabled": bool(k.get("disabled")),
                        "api_key": bool(token and token.startswith("sk-ant-api"))})
        return out

    @locked
    def add(self, name: str, token: str) -> None:
        data = self.read()
        if any(k["name"] == name for k in data["keys"]):
            raise BadRequest(f"key {name} already exists")
        folder = os.path.join(os.path.dirname(self.index), "keys")
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, f"{name}.key")
        if os.path.exists(path):
            raise BadRequest(f"{path} already exists")
        write_atomic(path, token + "\n", 0o600)
        priority = max((k["priority"] for k in data["keys"]), default=0) + 1
        data["keys"].append({"priority": priority, "name": name, "path": path})
        self.save(data)

    def path(self, name: str) -> str:
        """The key's token file."""
        entry = next((k for k in self.read()["keys"] if k["name"] == name), None)
        if entry is None:
            raise NotFound(f"no key named {name}")
        return os.path.expanduser(str(entry["path"]))

    @locked
    def set_token(self, name: str, token: str) -> None:
        write_atomic(self.path(name), token + "\n", 0o600)

    def open(self, name: str) -> None:
        path = self.path(name)
        if not os.path.exists(path):
            raise NotFound(f"the token file of {name} is missing")
        self.open_file(path)

    @locked
    def remove(self, name: str) -> None:
        data = self.read()
        entry = next((k for k in data["keys"] if k["name"] == name), None)
        if entry is None:
            raise NotFound(f"no key named {name}")
        data["keys"].remove(entry)
        for i, k in enumerate(sorted(data["keys"], key=lambda k: k["priority"])):
            k["priority"] = i + 1
        self.save(data)  # index first, so the proxy never picks a key whose file is gone
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.expanduser(entry["path"]))

    @locked
    def set_disabled(self, name: str, disabled: bool) -> None:
        data = self.read()
        entry = next((k for k in data["keys"] if k["name"] == name), None)
        if entry is None:
            raise NotFound(f"no key named {name}")
        if disabled:
            entry["disabled"] = True
        else:
            entry.pop("disabled", None)
        self.save(data)

    @locked
    def order(self, names: list[str]) -> None:
        data = self.read()
        if sorted(names) != sorted(k["name"] for k in data["keys"]):
            raise BadRequest("order must list every key once")
        rank = {n: i + 1 for i, n in enumerate(names)}
        for k in data["keys"]:
            k["priority"] = rank[k["name"]]
        self.save(data)


def merge_usage(keys: list[Json], usage: dict[str, Usage]) -> dict[str, Any]:
    """Usage per key name: what the proxy saw, None if nothing yet, "n/a" for API keys (no quota)."""
    return {k["name"]: "n/a" if k["api_key"] else usage.get(k["name"]) for k in keys}


def find_claude() -> str | None:
    """The claude command: KEYPROXY_CLAUDE if set, else claude from PATH."""
    return os.environ.get("KEYPROXY_CLAUDE") or shutil.which("claude")


class Signins:
    """The one `claude setup-token` run that may be open at a time."""

    def __init__(self, keys: Keys, open_session: Callable[[str], setup_token.Session] = setup_token.Session,
                 claude: Callable[[], str | None] = find_claude) -> None:
        """`open_session(claude)` starts a sign-in and `claude()` finds the command; tests can pass fakes."""
        self.keys = keys
        self.open_session = open_session
        self.claude = claude
        self.lock = threading.Lock()
        self.current: tuple[str, setup_token.Session, str, bool] | None = None  # (id, session, name, replace)

    def check(self, name: object, replace: bool) -> str:
        """The name, once it is valid and exists (replace) or does not exist yet (add)."""
        checked = valid_name(name)
        exists = any(k["name"] == checked for k in self.keys.read()["keys"])
        if replace and not exists:
            raise NotFound(f"no key named {checked}")
        if not replace and exists:
            raise BadRequest(f"key {checked} already exists")
        return checked

    def start(self, name: object, replace: bool) -> dict[str, str]:
        checked = self.check(name, replace)
        claude = self.claude()
        if not claude:
            raise BadRequest("the claude command was not found. Paste a token instead.")
        self.cancel()
        try:
            session = self.open_session(claude)
            link = session.url()
        except (OSError, setup_token.SetupError) as e:
            raise BadRequest(str(e)) from e
        sid = secrets.token_urlsafe(16)
        with self.lock:
            self.current = (sid, session, checked, replace)
        return {"id": sid, "url": link}

    def take(self, sid: object) -> tuple[setup_token.Session, str, bool]:
        with self.lock:
            if not self.current or not secrets.compare_digest(self.current[0], str(sid)):
                raise BadRequest("this sign-in is no longer open. Start again.")
            _, session, name, replace = self.current
            self.current = None
        return session, name, replace

    def finish(self, sid: object, code: object) -> None:
        if not isinstance(code, str) or not CODE.fullmatch(code.strip()):
            raise BadRequest("paste the whole code from the sign-in page")
        session, name, replace = self.take(sid)
        try:
            token = session.token(code.strip())
        except setup_token.SetupError as e:
            raise BadRequest(str(e)) from e
        if replace:
            self.keys.set_token(name, token)
        else:
            self.keys.add(name, token)

    def cancel(self, sid: object = None) -> None:
        with self.lock:
            cur = self.current
            if cur and (sid is None or secrets.compare_digest(cur[0], str(sid))):
                self.current = None
            else:
                cur = None
        if cur:
            cur[1].close()


api = Router()  # every JSON endpoint; each gets the server, the request body and the path params


@api.route("GET", f"{API}/keys")
def list_keys(server: ProxyServer, _body: None) -> list[Json]:
    return server.keys.all()


@api.route("POST", f"{API}/keys", status=201)
def add_key(server: ProxyServer, body: Json) -> list[Json]:
    token = valid_token(body.get("token"))
    server.keys.add(valid_name(body.get("name")), token)
    return server.keys.all()


@api.route("PUT", f"{API}/keys/<name>")
def replace_token(server: ProxyServer, body: Json, name: str) -> list[Json]:
    server.keys.set_token(name, valid_token(body.get("token")))
    return server.keys.all()


@api.route("DELETE", f"{API}/keys/<name>")
def delete_key(server: ProxyServer, _body: None, name: str) -> list[Json]:
    server.keys.remove(name)
    return server.keys.all()


@api.route("POST", f"{API}/keys/<name>/open")
def open_token_file(server: ProxyServer, _body: Json, name: str) -> Json:
    server.keys.open(name)
    return {}


@api.route("PUT", f"{API}/keys/<name>/disabled")
def set_disabled(server: ProxyServer, body: Json, name: str) -> list[Json]:
    if not isinstance(body.get("disabled"), bool):
        raise BadRequest("disabled must be true or false")
    server.keys.set_disabled(name, body["disabled"])
    if body["disabled"]:
        server.pool.cooldowns.clear(name)
        server.pool.forget(name)
    return server.keys.all()


@api.route("PUT", f"{API}/order")
def set_order(server: ProxyServer, body: Json) -> list[Json]:
    if not isinstance(body.get("order"), list):
        raise BadRequest("order must be a list of names")
    server.keys.order(body["order"])
    return server.keys.all()


@api.route("GET", f"{API}/cache")
def cooldowns(server: ProxyServer, _body: None) -> dict[str, int]:
    return server.pool.cooldowns.status()


@api.route("POST", f"{API}/cache/reset")
def reset_cooldowns(server: ProxyServer, _body: None) -> dict[str, int]:
    server.pool.reset()
    return server.pool.cooldowns.status()


@api.route("GET", f"{API}/usage")
def usage(server: ProxyServer, _body: None) -> dict[str, Any]:
    return merge_usage(server.keys.all(), server.pool.seen())


@api.route("POST", f"{API}/usage/probe")
def probe_usage(server: ProxyServer, _body: Json) -> dict[str, Any]:
    server.probe_all()
    return usage(server, None)


@api.route("POST", f"{API}/setup/start")
def start_sign_in(server: ProxyServer, body: Json) -> dict[str, str]:
    return server.signins.start(body.get("name"), body.get("replace") is True)


@api.route("POST", f"{API}/setup/finish")
def finish_sign_in(server: ProxyServer, body: Json) -> list[Json]:
    server.signins.finish(body.get("id"), body.get("code"))
    return server.keys.all()


@api.route("POST", f"{API}/setup/cancel")
def cancel_sign_in(server: ProxyServer, body: Json) -> Json:
    server.signins.cancel(body.get("id"))
    return {}


def owns(path: str) -> bool:
    return path == "/" or path.startswith(PREFIX)


def local(handler: Handler) -> bool:
    """Host and Origin must name this server, so no other site can read or write keys."""
    port = handler.server.server_port
    own = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origin = handler.headers.get("origin")
    # the page is served over plain HTTP on 127.0.0.1 only, so that is the origin to expect
    return (handler.headers.get("host") in own
            and (origin is None or origin in {f"http://{h}" for h in own}))  # NOSONAR


def answer(handler: Handler) -> None:
    """Answer a request for the page or the API. `handler` is a JsonHandler on a server with keys,
    signins, pool and headless. A headless server answers only the read-only GET calls."""
    if not local(handler):
        return handler.send_json(403, {"error": "forbidden"})
    if handler.server.headless and (handler.path in FILES or handler.command != "GET"):
        return handler.send_json(404, {"error": "the key manager is off, the proxy runs with --headless"})
    if handler.command == "GET" and handler.path in FILES:
        name, ctype = FILES[handler.path]
        with open(os.path.join(UI_DIR, name), "rb") as f:
            return handler.send_body(200, f.read(), ctype)
    with_body = handler.command in ("POST", "PUT")
    if with_body and not handler.has_json():
        return handler.send_json(415, {"error": "send application/json"})
    try:
        found = api.match(handler.command, handler.path)
        if found is None:
            raise NotFound("not found")
        fn, params, status = found
        handler.send_json(status, fn(handler.server, handler.read_json() if with_body else None, **params))
    except tuple(kind for kind, _ in ERRORS) as e:
        handler.send_json(next(code for kind, code in ERRORS if isinstance(e, kind)), {"error": str(e)})
