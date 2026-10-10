# Linkkeep development tasks.  `make` alone prints the help.
PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
RUFF ?= .venv/bin/ruff
MYPY ?= .venv/bin/mypy
COVERAGE_MIN ?= 85
DEMO_ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help venv install test lint fmt typecheck coverage build clean goldens demo check all

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create .venv with the right Python
	@python3 -m venv .venv
	@$(PIP) install --upgrade pip

install: ## Install Linkkeep in editable mode with dev extras + git hooks
	$(PIP) install -e ".[dev]"
	@$(PY) -m pre_commit install || echo "pre-commit not installed here; run: $(PY) -m pre_commit run --all-files"

test: ## Run the test suite (with coverage gate)
	$(PY) -m pytest -q --cov-fail-under=$(COVERAGE_MIN)

lint: ## ruff check
	$(RUFF) check src tests tools

fmt: ## ruff format (write)
	$(RUFF) format src tests tools
	$(RUFF) check --fix src tests tools

fmt-check: ## ruff format --check
	$(RUFF) format --check src tests tools

typecheck: ## mypy (strict, src only)
	$(MYPY)

coverage: ## HTML coverage report in ./htmlcov
	$(PY) -m pytest -q --cov=linkkeep --cov-report=html
	@echo "open htmlcov/index.html"

goldens: ## Regenerate report golden files
	$(PY) -m tests.update_goldens

demo: ## Run the real CLI against the bundled fixture site (DEMO_ARGS=--save to keep output)
	$(PY) tools/demo.py $(DEMO_ARGS)

build: ## Build sdist + wheel into ./dist
	$(PY) -m build

clean: ## Remove build and cache artefacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

check: lint fmt-check typecheck test ## Everything CI runs

all: install check ## Install then check
