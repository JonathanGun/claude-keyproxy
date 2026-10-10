"""Local proxy in front of api.anthropic.com that picks the OAuth key per request.

Point Claude Code at it. The incoming token is dropped and replaced with a key from the pool:

    ANTHROPIC_BASE_URL=http://127.0.0.1:18787 claude

Keys come from index.json and are tried in the order a strategy gives (see strategies/),
by default by priority (lowest number first). A key that answers 429, 401 or "rejected" is skipped
until its limit resets, and the request is retried on the next key. The session id and conversation are untouched.
Requests that carry an x-api-key and no Authorization header are API-key traffic
and are forwarded as they are.

The same process serves the key manager page at / and its API under /_keyproxy/, see
manager.py. Nothing under those paths is forwarded. --headless turns the page off.

This file starts the server. The rest lives in:
    forward.py      the request handler: forwarding, failover, relaying the response
    keypool.py      the keys, their cooldowns and their usage
    strategies/     which key a request tries first
    manager.py      the key manager page and API
"""
from __future__ import annotations

import argparse
import http.client
import os
from urllib.parse import urlsplit

from . import __version__, manager, strategies
from .forward import Handler, ProxyServer, log
from .keypool import KeyPool

DEFAULT_INDEX = os.path.expanduser("~/.config/claude/index.json")
DEFAULT_UPSTREAM = "https://api.anthropic.com"
DEFAULT_PORT = 18787


def make_server(port: int, index: str = DEFAULT_INDEX, upstream: str = DEFAULT_UPSTREAM,
                host: str = "127.0.0.1", strategy: strategies.Strategy | None = None,
                headless: bool = False, pool: KeyPool | None = None,
                signins: manager.Signins | None = None) -> ProxyServer:
    """The server, ready for serve_forever(). Pass `pool` or `signins` to swap in your own, e.g. in tests."""
    server = ProxyServer((host, port), Handler)
    server.headless = headless
    server.pool = pool or KeyPool(index, strategy)
    server.signins = signins or manager.Signins(manager.Keys(index))
    server.keys = server.signins.keys
    server.target = urlsplit(upstream)
    if server.target.scheme == "https":
        server.connect = http.client.HTTPSConnection
    else:
        server.connect = http.client.HTTPConnection
    return server


def main() -> None:
    parser = argparse.ArgumentParser(prog="keyproxy",
                                     description="Claude OAuth key failover proxy, run in the foreground")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--port", type=int, default=int(os.environ.get("KEYPROXY_PORT", DEFAULT_PORT)))
    parser.add_argument("--index", default=os.environ.get("CLAUDE_KEY_INDEX", DEFAULT_INDEX))
    parser.add_argument("--upstream", default=os.environ.get("KEYPROXY_UPSTREAM", DEFAULT_UPSTREAM))
    parser.add_argument("--strategy", choices=strategies.names(),
                        default=os.environ.get("KEYPROXY_STRATEGY", strategies.DEFAULT),
                        help="how keys are chosen, a file in claude_keyproxy/strategies/ (default: %(default)s)")
    parser.add_argument("--headless", action="store_true", default=os.environ.get("KEYPROXY_HEADLESS") == "1",
                        help="no key manager page: keys are only changed by editing the files")
    args = parser.parse_args()
    try:
        strategy = strategies.load(args.strategy)  # also checks a KEYPROXY_STRATEGY that skipped choices
    except ValueError as e:
        parser.error(str(e))
    server = make_server(args.port, args.index, args.upstream, strategy=strategy, headless=args.headless)
    page = "off (headless)" if args.headless else f"http://127.0.0.1:{args.port}/"
    log(f"keyproxy on http://127.0.0.1:{args.port}, key manager {page}, "
        f"strategy {args.strategy}, keys: {[k.name for k in server.pool.candidates()]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return  # Ctrl-C stops the proxy


if __name__ == "__main__":
    main()
