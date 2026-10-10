"""HTTP plumbing shared by the proxy and the key manager: a JSON request handler and a router.

    api = Router()

    @api.route("PUT", "/api/keys/<name>")
    def replace_token(server, body, name):
        ...
        return {"ok": True}      # sent back as JSON with the route's status

    fn, params, status = api.match("PUT", "/api/keys/work")
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, TypeVar

Route = Callable[..., Any]  # fn(server, body, **path params) -> data sent back as JSON
R = TypeVar("R", bound=Route)


class JsonHandler(BaseHTTPRequestHandler):
    extra_headers: tuple[tuple[str, str], ...] = ()  # (name, value) pairs sent on every response

    def log_message(self, format: str, *args: Any) -> None:
        pass  # quiet: each server logs what it needs itself

    def send_body(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        for name, value in self.extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, data: Any) -> None:
        self.send_body(code, json.dumps(data).encode(), "application/json")

    def has_json(self) -> bool:
        return self.headers.get("content-type", "").split(";")[0].strip() == "application/json"

    def read_json(self) -> dict[str, Any]:
        """The request body as a JSON object. ValueError if it is not one."""
        data = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data


class Router:
    """(method, path pattern) -> function. A <name> part of a pattern matches one path segment."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Route, int]] = []  # (method, regex, function, status)

    def route(self, method: str, pattern: str, status: int = 200) -> Callable[[R], R]:
        regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")

        def register(fn: R) -> R:
            self.routes.append((method, regex, fn, status))
            return fn
        return register

    def match(self, method: str, path: str) -> tuple[Route, dict[str, str], int] | None:
        """(function, path params, status) of the route that fits, or None."""
        for route_method, regex, fn, status in self.routes:
            hit = regex.match(path)
            if hit and route_method == method:
                return fn, hit.groupdict(), status
        return None
