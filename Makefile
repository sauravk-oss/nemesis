# Nemesis v2 — Makefile
# Thin wrappers over setup.sh and the brain CLI. Run `make` (or `make help`) for the list.

PYTHON ?= python3

.DEFAULT_GOAL := help
.PHONY: help setup check init doctor sources experts test clean

help:  ## Show this help
	@echo "Nemesis v2 — make targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

setup:  ## Full idempotent setup (deps + MCP validate + brain init)
	./setup.sh

check:  ## Read-only diagnostics (no installs, no writes)
	./setup.sh --check

init:  ## Initialize brain.db (seeds services + skill registry)
	$(PYTHON) -m brain init

sources:  ## Register data sources from config/sources.json
	$(PYTHON) -m brain register-sources

experts:  ## Seed project experts to L1
	$(PYTHON) -m brain init-experts --level 1

doctor:  ## Health check (deps, brain.db, sources, experts, MCPs)
	$(PYTHON) -m brain doctor

test:  ## Compile-check Python + smoke-test the new scripts
	@echo "==> py_compile brain/ + scripts/"
	@$(PYTHON) -m py_compile $$(find brain scripts -name '*.py' -not -path '*/_archive/*') && echo "    compile OK"
	@echo "==> brain CLI loads"
	@$(PYTHON) -m brain --help >/dev/null && echo "    brain --help OK"
	@echo "==> report + sync scripts load"
	@$(PYTHON) scripts/feature_sync.py --help >/dev/null && echo "    feature_sync --help OK"
	@$(PYTHON) scripts/test_report.py --help >/dev/null && echo "    test_report --help OK"
	@$(PYTHON) scripts/change_report.py --help >/dev/null && echo "    change_report --help OK"
	@$(PYTHON) scripts/pipeline_report.py build --help >/dev/null && echo "    pipeline_report build --help OK"

clean:  ## Remove __pycache__, *.pyc, and stray root SQLite files
	@find . -type d -name __pycache__ -not -path './workspace/*' -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -not -path './workspace/*' -delete 2>/dev/null || true
	@rm -f ./init ./stats ./--help* ./--type 2>/dev/null || true
	@echo "cleaned"
