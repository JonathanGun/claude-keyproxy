.PHONY: help test lint typecheck check run install uninstall

help:      ## show targets
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

test:      ## run the unit tests
	python3 -m unittest discover -s tests

lint:      ## ruff, through uvx so it is not a dependency
	uvx ruff check .

typecheck: ## mypy --strict, through uvx
	uvx mypy

check: lint typecheck test  ## everything CI runs

run:       ## start the proxy and key manager in the foreground on 127.0.0.1:18787
	PYTHONPATH=src python3 -m claude_keyproxy

install:   ## install the commands from this clone, editable, with uv
	uv tool install --editable --force .

uninstall: ## remove the installed commands
	uv tool uninstall claude-keyproxy
