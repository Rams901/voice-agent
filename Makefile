# Frontdesk — developer tasks.
#
#   make            list targets
#   make check      the same gate CI runs (lint + tests)
#
# Targets marked [API] call Retell and need RETELL_API_KEY in .env.

PY ?= python3
.DEFAULT_GOAL := help

.PHONY: help install test lint fmt check plan adapters diagram simulate \
        benchmark doctor deploy webcall cleanup clean

help:  ## show this list
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- setup ----------------------------------------------------------------- #

install:  ## install runtime + dev dependencies
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install "ruff>=0.5"

# --- the gate -------------------------------------------------------------- #

test:  ## run the test suite (no credentials, no network)
	$(PY) -m pytest

lint:  ## ruff check
	ruff check .

fmt:  ## ruff autofix, then review the diff
	ruff check . --fix

check: lint test  ## what CI runs — use before every push

# --- no credentials needed ------------------------------------------------- #

plan:  ## architecture, prompt budget, dealer matrix
	$(PY) -m frontdesk.cli plan

adapters:  ## DMS capability matrix + adapter conformance suite
	$(PY) -m frontdesk.cli adapters --conformance

diagram:  ## serve the flow diagram on :8100
	$(PY) -m frontdesk.cli diagram

# --- [API] ----------------------------------------------------------------- #

doctor:  ## check credentials and connectivity
	$(PY) -m frontdesk.cli doctor

deploy:  ## serve tool server, open a tunnel, deploy the flow and agent
	./scripts/dev_tunnel.sh --deploy

webcall:  ## start a browser call against the deployed agent
	$(PY) -m frontdesk.cli webcall

simulate:  ## run the regression suite against the deployed flow
	$(PY) -m frontdesk.cli simulate

benchmark:  ## compare the monolith and modular architectures (budget-capped)
	$(PY) -m frontdesk.cli benchmark

cleanup:  ## delete the deployed flow and agent
	$(PY) -m frontdesk.cli cleanup

# --- housekeeping ---------------------------------------------------------- #

clean:  ## remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.py[cod]' -delete
