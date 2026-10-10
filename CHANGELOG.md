# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Check usage of all keys**, in a menu next to the refresh button. It sends a 1-token request with every key to read its usage, after a confirmation.
- A chip for Anthropic's status: "near the limit" or "rate limited".
- The page works on narrow screens. Each key folds into one line with its usage, and reordering works by touch.

### Changed

- Reordering uses pointer events instead of HTML5 drag and drop, so it works on touch screens.
- The last 4 characters of a token show only in the "New token" dialog. A button there opens the token file in your text editor.
- **Reset cache** asks for confirmation first.
- Success messages are gone. Only errors show below the list.

## [0.1.0]

### Added

- A local proxy that gives Claude Code several OAuth keys and moves a request to the next key when one is rate limited.
- A key manager page on the same port. Add a key by signing in with Claude, replace, delete or reorder keys, and see each key's usage.
- An MCP server, `keyproxy-mcp`, so an agent can manage the keys.
- `--headless` to run without the page, and `--strategy` to choose how a key is picked.
- The commands `keyproxy`, `keyproxy-up` and `claude-keyproxy`, installed with `uv tool install` or `pipx install`.
