# AGENTS.md

Notes for coding agents and contributors. The user guide is [README.md](README.md).

## Installation

Follow these steps when a user asks you to install claude-keyproxy. Run each command yourself and check its result before the next step. Ask the user before step 5.

1. **Check the requirements.** `python3 --version` must be 3.9 or newer, and `command -v claude` must find the Claude Code CLI. Stop and tell the user if either is missing. You need `uv` or `pipx` for step 2: prefer `uv` if `command -v uv` finds it.

2. **Install the package.** It puts `keyproxy-up`, `claude-keyproxy`, `keyproxy-mcp` and `keyproxy` on the `PATH`.

   ```bash
   uv tool install git+https://github.com/JonathanGun/claude-keyproxy
   # or: pipx install git+https://github.com/JonathanGun/claude-keyproxy
   ```

   If `command -v keyproxy-up` finds nothing afterwards, run `uv tool update-shell` or `pipx ensurepath`. Until the user opens a new terminal, use the full path from the install output.

3. **Start the proxy.** It runs in the background and serves the key manager page too.

   ```bash
   keyproxy-up
   curl -s http://127.0.0.1:18787/_keyproxy/api/keys    # prints [] on a new install
   ```

4. **Have the user add their accounts.** Run `keyproxy-up --open` to open the page. If no browser opens, give them http://127.0.0.1:18787/. They click **Add a new key**, then **Sign in with Claude**. They repeat this for each account. Do not run `claude setup-token` yourself, because the sign-in happens in the user's browser. Wait until the user says they are done, then confirm the keys exist:

   ```bash
   curl -s http://127.0.0.1:18787/_keyproxy/api/keys
   ```

5. **Send `claude` through the proxy.** Ask the user which they want.
   - **Only on demand.** They run `claude-keyproxy` in place of `claude`. Nothing else to change.
   - **For every `claude`.** Find the shell startup file from `$SHELL`: `~/.zshrc` for zsh, `~/.bashrc` for bash. Show the user these lines, then append them:

     ```sh
     export ANTHROPIC_BASE_URL=http://127.0.0.1:18787
     claude() { keyproxy-up && command claude "$@"; }
     ```

     Claude Code must be logged in once, with any account, for this to work. If `claude` asks for a login, the user runs `/login` once. The proxy replaces that login's token on every request.

6. **Offer the MCP server.** With it, an agent can manage the keys later. Ask first, then run `claude mcp add keyproxy -- keyproxy-mcp`.

7. **Check that it works.**

   ```bash
   keyproxy-up
   curl -s http://127.0.0.1:18787/_keyproxy/api/cache    # prints JSON, {} when no key is cooling down
   ```

   Tell the user that the change applies to new terminals and new Claude Code sessions, not to the session you are running in. The proxy log is `~/.cache/keyproxy.log`.

If the user already has tokens and does not want the page, write them as files instead. Put one token per file in `~/.config/claude/keys/<name>.key` with mode 600. List the files in `~/.config/claude/index.json`, in the shape of `examples/index.json`. Then go on from step 5.

## Layout

| Path | What |
| --- | --- |
| `src/claude_keyproxy/server.py` | the `keyproxy` command: options, and `make_server()` |
| `src/claude_keyproxy/cli.py` | the `keyproxy-up` and `claude-keyproxy` commands: start, restart, open the page, run `claude` |
| `src/claude_keyproxy/forward.py` | `Handler`: forwards a request with one key after another, and relays the response. `ProxyServer` declares the state it reads |
| `src/claude_keyproxy/keypool.py` | `KeyPool` reads the keys, `Cooldowns` tracks limited keys, `parse_usage()` reads the rate limit headers |
| `src/claude_keyproxy/strategies/` | which keys a request tries, one file per strategy. `priority.py` is the default |
| `src/claude_keyproxy/manager.py` | the key manager page and API, served by the proxy under `/_keyproxy/`. `Keys` edits the files, `Signins` owns the open sign-in, and each endpoint is a function registered with `@api.route` |
| `src/claude_keyproxy/mcp.py` | the `keyproxy-mcp` command: an MCP server over stdin and stdout, a thin client of the key manager API |
| `src/claude_keyproxy/setup_token.py` | runs `claude setup-token`: `Pty` drives the terminal, `find_link` and `find_token` parse its output, `Session` ties them together |
| `src/claude_keyproxy/http_json.py` | `JsonHandler`, the base of the request handler, and the `Router` behind `@api.route` |
| `src/claude_keyproxy/ui/` | the key manager page, shipped inside the package. Every color is in `theme.css`, a light and a dark palette. Alpine.js and Pico.css are vendored and minified, never edit them |
| `bin/` | `keyproxy-up` and `claude-keyproxy` for a clone that is not installed. They only set `PYTHONPATH` and call `cli.py` |
| `tests/` | unit tests, one file per module. `server.py` and `http_json.py` have none, because every server test goes through them |
| `docs/*.png` | the README screenshots |

## Commands

```bash
make check      # lint, typecheck and test: what CI runs
make test       # unit tests, no network and no real keys
make lint       # ruff, through uvx
make typecheck  # mypy --strict, through uvx
make run        # the proxy and key manager in the foreground on 127.0.0.1:18787
make install    # install this clone with uv, editable, so the commands run your changes
```

Every function has type hints on its arguments and return value. Keep `make check` clean. CI runs it on every push and pull request, with the tests on Python 3.9 to 3.13.

To release, bump `__version__` in `src/claude_keyproxy/__init__.py`, move the Unreleased notes in `CHANGELOG.md` under the new version, and tag the commit `v<version>`.

Use a throwaway `CLAUDE_KEY_INDEX` and other ports when you run anything by hand. Never point a test at the user's real `~/.config/claude`.

Two more variables exist for tests and development. The user-facing ones are in the README.

| Variable | Default | Does |
| --- | --- | --- |
| `KEYPROXY_CLAUDE` | `claude` from `PATH` | the command the key manager runs to sign in |
| `KEYPROXY_UPSTREAM` | `https://api.anthropic.com` | where the proxy forwards requests |

## Rules

- Python standard library only at run time. No new dependency, in Python or in `ui/`. Dev tools run through `uvx`.
- Tokens never leave the server. The API returns the last 4 characters only. Key files are mode 600, and `index.json` is written to a temp file and renamed.
- Writes to the key manager API must be JSON from the page's own origin. Keep the Host and Origin check in `manager.local()`.
- Nothing under `/_keyproxy/` or at `/` is forwarded upstream.
- An MCP tool calls the key manager API. It never edits files itself, so the API's checks apply to agents too.
- Never run the real `claude setup-token` in a test. It opens a browser and starts a real sign-in. Use a fake: the fake CLI in `tests/test_manager.py` through `KEYPROXY_CLAUDE`, or a fake session passed to `Signins`.
- The README is for users. Development notes belong here.
- Commit messages are one subject line, nothing else.

## Key manager API

Served by the proxy itself, from `src/manager.py`, on 127.0.0.1:18787. `--headless` keeps only the `GET` calls.

| Call | Does |
| --- | --- |
| `GET /_keyproxy/api/keys` | keys by priority, with the last 4 characters of each token |
| `POST /_keyproxy/api/keys` `{"name","token"}` | adds a key at the bottom |
| `PUT /_keyproxy/api/keys/<name>` `{"token"}` | replaces that key's token |
| `DELETE /_keyproxy/api/keys/<name>` | deletes the key and its token file |
| `POST /_keyproxy/api/keys/<name>/open` `{}` | opens the token file in the default text editor. The token never comes back through the API |
| `PUT /_keyproxy/api/keys/<name>/disabled` `{"disabled"}` | turns the key off, so the proxy skips it, or back on. Saved in `index.json` |
| `POST /_keyproxy/api/setup/start` `{"name","replace"}` | runs `claude setup-token` and returns the sign-in link |
| `POST /_keyproxy/api/setup/finish` `{"id","code"}` | gives the code to `claude` and saves the token it returns |
| `POST /_keyproxy/api/setup/cancel` `{"id"}` | stops a sign-in |
| `PUT /_keyproxy/api/order` `{"order":[names]}` | sets the priority to that order |
| `GET /_keyproxy/api/cache` | the proxy's cooldowns, seconds left per key |
| `POST /_keyproxy/api/cache/reset` `{}` | clears the cooldowns and the usage seen |
| `GET /_keyproxy/api/usage` | 5-hour and weekly usage per key |
| `POST /_keyproxy/api/usage/probe` `{}` | sends a 1-token request with every subscription key, then returns the usage. It starts a 5-hour window |

## Adding a strategy

1. Add `src/claude_keyproxy/strategies/<name>.py` with `def choose(keys: list[Key], cooldowns: Cooldowns) -> list[Key]`. The contract is in `strategies/__init__.py`.
2. Return at least one key whenever `keys` is not empty, or the request fails with 503.
3. Add tests to `tests/test_strategies.py`. Build the cooldowns with a fixed clock, `Cooldowns(clock=lambda: 0.0)`.
4. Run it with `keyproxy-up --restart --strategy <name>`.

## Testing with fakes

Each class takes what it depends on, with the real thing as the default:

| Class | Pass a fake for |
| --- | --- |
| `Cooldowns(clock=...)` | the time, so a test moves it by hand |
| `KeyPool(index, strategy=..., cooldowns=...)` | the strategy and the cooldowns |
| `Signins(keys, open_session=..., claude=...)` | the sign-in session and the `claude` lookup, so no process starts |
| `Session(claude, spawn=...)` | the pseudo-terminal |
| `make_server(..., pool=..., signins=...)` | the pool and sign-ins of a whole server |
| `mcp.Api(port, ensure=...)` | starting the proxy when it is down |

## Checking the UI in headless Chrome

- Pico animates button colors. A screenshot taken during the transition shows the old color, so inject `*{transition:none!important}` before you judge a color.
- Alpine's `x-show` reveals an element on the next animation frame. Code that focuses or measures it must wait for `requestAnimationFrame` after `$nextTick`.
- With `--virtual-time-budget`, the dialog `close` event may never fire. Check close behaviour in a real browser.
- Chrome's window has a minimum width of about 500px. To check a phone layout, load the page in a 390px wide `iframe`.
