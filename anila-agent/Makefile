# anila-agent — developer convenience targets.
#
# Quickstart for a new agent built from this template:
#   make install        # editable install + serving + dev extras into .venv
#   make test           # run the test suite
#   make serve          # run the OpenAI-compatible service wrapper (port 8200)
#   make docker-build && make docker-run
#
# All targets use a local .venv (created on `make install`).

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
PORT ?= 8200
IMAGE ?= anila-agent:dev

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip

.PHONY: install
install: $(BIN)/python ## Editable install with serving + dev extras
	$(BIN)/pip install -e '.[serving,dev]'

.PHONY: test
test: ## Run the test suite
	$(BIN)/python -m pytest -q

.PHONY: lint
lint: ## Ruff lint
	$(BIN)/ruff check anila_agent tests

.PHONY: run
run: ## Run the interactive CLI agent
	$(BIN)/anila

.PHONY: serve
serve: ## Serve the OpenAI-compatible wrapper (CSP-dispatchable)
	$(BIN)/uvicorn examples.service_wrapper:app --host 0.0.0.0 --port $(PORT)

.PHONY: docker-build
docker-build: ## Build the agent service image
	docker build -t $(IMAGE) .

.PHONY: docker-run
docker-run: ## Run the agent service image (honours .env if present)
	docker run --rm -p $(PORT):$(PORT) --env-file .env $(IMAGE)

.PHONY: clean
clean: ## Remove caches + venv
	rm -rf $(VENV) .pytest_cache .ruff_cache **/__pycache__
