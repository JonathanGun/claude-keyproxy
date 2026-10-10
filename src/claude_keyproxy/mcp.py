"""An MCP server, so an agent can manage the keys: list, add, replace, delete, reorder, reset.

    claude mcp add keyproxy -- keyproxy-mcp

It speaks JSON-RPC over stdin and stdout, one message per line, and calls the running proxy's
key manager API, starting the proxy if it is down. The API does the checks, so the page and an
agent follow the same rules. Tokens never come back: the API only shows their last 4 characters.
"""
from __future__ import annotations

import http.client
import json
import sys
from typing import IO, Any, Callable, NamedTuple
from urllib.parse import quote

from . import __version__, cli

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
API = "/_keyproxy/api"
Json = dict[str, Any]


class ToolError(Exception):
    """The tool ran and failed. The agent sees the message."""


class Api:
    """The running proxy's key manager API. `ensure()` starts the proxy, and is a fake in tests."""

    def __init__(self, port: int, ensure: Callable[[], bool] = cli.ensure) -> None:
        self.port = port
        self.ensure = ensure

    def call(self, method: str, path: str, body: Json | None = None) -> Any:
        try:
            return self.send(method, path, body)
        except ConnectionRefusedError:
            if not self.ensure():
                raise ToolError(f"keyproxy is not running and did not start on port {self.port}") from None
            return self.send(method, path, body)

    def send(self, method: str, path: str, body: Json | None) -> Any:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)  # a sign-in can take a while
        try:
            headers = {"content-type": "application/json"} if body is not None else {}
            conn.request(method, API + path, json.dumps(body) if body is not None else None, headers)
            resp = conn.getresponse()
            data = json.loads(resp.read() or b"null")
        finally:
            conn.close()
        if resp.status >= 400:
            raise ToolError(data.get("error", resp.reason) if isinstance(data, dict) else resp.reason)
        return data


class Tool(NamedTuple):
    name: str
    description: str
    params: Json  # JSON Schema properties
    required: tuple[str, ...]
    run: Callable[[Api, Json], Any]


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, params: Json | None = None,
         required: tuple[str, ...] = ()) -> Callable[[Callable[[Api, Json], Any]], Callable[[Api, Json], Any]]:
    def register(run: Callable[[Api, Json], Any]) -> Callable[[Api, Json], Any]:
        TOOLS[name] = Tool(name, description, params or {}, required, run)
        return run
    return register


NAME = {"type": "string", "description": "The key's name: letters, digits, '.', '_' and '-'"}
TOKEN = {"type": "string", "description": "An OAuth token from `claude setup-token`, starting with sk-ant-"}
TOKEN_WARNING = (" The token passes through this conversation, so prefer start_sign_in, which keeps it on this machine."
                 " Use this only when the user hands you a token on purpose.")


@tool("list_keys", "The keys by priority, with the last 4 characters of each token, which keys are cooling "
      "down (seconds left), and the 5-hour and weekly usage of each key.")
def list_keys(api: Api, _args: Json) -> Json:
    return {"keys": api.call("GET", "/keys"), "cooldowns": api.call("GET", "/cache"),
            "usage": api.call("GET", "/usage")}


@tool("add_key", "Add a key at the lowest priority from a token." + TOKEN_WARNING,
      {"name": NAME, "token": TOKEN}, ("name", "token"))
def add_key(api: Api, args: Json) -> Any:
    return api.call("POST", "/keys", {"name": args.get("name"), "token": args.get("token")})


@tool("replace_token", "Replace the token of an existing key." + TOKEN_WARNING,
      {"name": NAME, "token": TOKEN}, ("name", "token"))
def replace_token(api: Api, args: Json) -> Any:
    return api.call("PUT", f"/keys/{quote(str(args['name']), safe='')}", {"token": args.get("token")})


@tool("delete_key", "Delete a key and its token file. This cannot be undone: confirm with the user first.",
      {"name": NAME}, ("name",))
def delete_key(api: Api, args: Json) -> Any:
    return api.call("DELETE", f"/keys/{quote(str(args['name']), safe='')}")


@tool("reorder_keys", "Set the priority order. The first name is tried first. List every key exactly once.",
      {"order": {"type": "array", "items": {"type": "string"}, "description": "Every key name, first to last"}},
      ("order",))
def reorder_keys(api: Api, args: Json) -> Any:
    return api.call("PUT", "/order", {"order": args.get("order")})


@tool("reset_cooldowns", "Make every key ready again, for example after a limit was lifted early.")
def reset_cooldowns(api: Api, _args: Json) -> Any:
    return api.call("POST", "/cache/reset", {})


@tool("start_sign_in", "Start adding a key, or replacing a key's token, by signing in with Claude. Returns a "
      "sign-in link and an id. Give the user the link, ask them to sign in with the account for this key and "
      "paste back the code the page shows, then call finish_sign_in. The token never enters this conversation.",
      {"name": NAME, "replace": {"type": "boolean", "description": "true to replace an existing key's token"}},
      ("name",))
def start_sign_in(api: Api, args: Json) -> Any:
    return api.call("POST", "/setup/start", {"name": args.get("name"), "replace": args.get("replace") is True})


@tool("finish_sign_in", "Finish a sign-in with the code the user pasted. One try per sign-in: if the code is "
      "rejected, start again.", {"id": {"type": "string"}, "code": {"type": "string"}}, ("id", "code"))
def finish_sign_in(api: Api, args: Json) -> Any:
    return api.call("POST", "/setup/finish", {"id": args.get("id"), "code": args.get("code")})


@tool("cancel_sign_in", "Stop a sign-in that will not be finished.", {"id": {"type": "string"}}, ("id",))
def cancel_sign_in(api: Api, args: Json) -> Any:
    return api.call("POST", "/setup/cancel", {"id": args.get("id")})


def tool_list() -> list[Json]:
    return [{"name": t.name, "description": t.description,
             "inputSchema": {"type": "object", "properties": t.params, "required": list(t.required)}}
            for t in TOOLS.values()]


def call_tool(api: Api, params: Json) -> Json:
    found = TOOLS.get(params.get("name", ""))
    if found is None:
        return {"content": [{"type": "text", "text": f"unknown tool {params.get('name')!r}"}], "isError": True}
    args = params.get("arguments") or {}
    missing = [name for name in found.required if args.get(name) in (None, "")]
    if missing:
        return {"content": [{"type": "text", "text": f"missing argument: {', '.join(missing)}"}], "isError": True}
    try:
        result = found.run(api, args)
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "isError": False}


def handle(api: Api, message: Json) -> Json | None:
    """The reply to one JSON-RPC message, or None for a notification."""
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:
        return None  # notifications, e.g. notifications/initialized, get no reply
    if method == "initialize":
        asked = params.get("protocolVersion")
        result: Any = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": "claude-keyproxy", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        result = call_tool(api, params)
    else:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def serve(api: Api, stdin: IO[str], stdout: IO[str]) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: Json | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(api, message) if isinstance(message, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main() -> None:
    """keyproxy-mcp. Stdout is the protocol, so everything else goes to stderr."""
    serve(Api(cli.port()), sys.stdin, sys.stdout)
