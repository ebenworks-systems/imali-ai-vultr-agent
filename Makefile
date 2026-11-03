PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: venv test test-unit run worker parser check-copy clean

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --quiet --upgrade pip
	$(BIN)/python -m pip install --quiet -r requirements-dev.txt

venv: $(BIN)/python

# The whole suite against a throwaway Postgres, including the ledger balance tests.
test: $(BIN)/python
	REQUIRE_DB=1 bash scripts/testdb.sh $(BIN)/python -m pytest -q

# The tests that need no database.
test-unit: $(BIN)/python
	$(BIN)/python -m pytest -q -m "not db"

run: $(BIN)/python
	$(BIN)/python -m uvicorn app.main:app --host 127.0.0.1 --port 8030 --reload

worker: $(BIN)/python
	$(BIN)/python -m app.worker

parser: $(BIN)/python
	$(BIN)/python -m app.parser_sandbox

clean:
	rm -rf .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
