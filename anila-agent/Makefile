# anila-agent — 開發便利目標。
#
#   make install     # 建 .venv 並 editable 安裝（含 dev）
#   make test        # 跑單元測試（不連網）
#   make test-live   # 跑 live 測試（需 ANILA_BASE_URL 指向可用端點）
#   make run         # 啟動互動 CLI
#   make lint        # ruff

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.DEFAULT_GOAL := help

.PHONY: help
help: ## 顯示說明
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip

.PHONY: install
install: $(BIN)/python ## 建 venv + editable 安裝（含 dev extra）
	$(BIN)/pip install -e '.[dev]'

.PHONY: test
test: ## 單元測試（不連網）
	$(BIN)/python -m pytest -q -m 'not live'

.PHONY: test-live
test-live: ## live 端點測試（需 ANILA_BASE_URL）
	$(BIN)/python -m pytest -q -m live

.PHONY: run
run: ## 啟動互動 CLI
	$(BIN)/anila

.PHONY: serve
serve: ## 一鍵起 OpenAI 相容服務（python app.py，先 load .env）
	$(BIN)/python app.py

.PHONY: lint
lint: ## ruff lint
	$(BIN)/ruff check anila_agent tests

IMAGE ?= anila-agent
TAG ?= 1.0.0

.PHONY: docker-build
docker-build: ## build 開發環境 image（外網/有網路環境）
	docker build -t $(IMAGE):$(TAG) .

.PHONY: docker-save
docker-save: ## build + 存成 tar，供搬進 air-gap 內網 / MLSteam
	ANILA_IMAGE=$(IMAGE) ANILA_TAG=$(TAG) scripts/docker-ship.sh save

.PHONY: docker-run
docker-run: ## 本機起 JupyterLab（掛當前 repo 進 workspace）
	docker run --rm -p 8888:8888 -v "$(PWD)":/home/anila/work $(IMAGE):$(TAG)

.PHONY: clean
clean: ## 移除 venv 與快取
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache **/__pycache__
