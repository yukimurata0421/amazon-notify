.PHONY: setup setup-dev test coverage lint run-once run reauth dry-run test-discord validate-config health-check clean dist

VENV_BIN = .venv/bin
PYTHON = $(VENV_BIN)/python
PIP = $(VENV_BIN)/pip
CLI = $(VENV_BIN)/amazon-notify

setup:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install .

setup-dev:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e .[dev]

test:
	$(PYTHON) -m pytest -q

coverage:
	$(PYTHON) -m pytest -q --cov=amazon_notify --cov-report=term-missing --cov-report=xml

lint:
	$(PYTHON) -m compileall -q amazon_notify

run-once:
	$(CLI) --once

run:
	$(CLI)

reauth:
	$(CLI) --reauth

dry-run:
	$(CLI) --once --dry-run

test-discord:
	$(CLI) --test-discord

validate-config:
	$(CLI) --validate-config

health-check:
	$(CLI) --health-check

clean:
	find . -type d -name '__pycache__' -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
	find . -type d -name '*.egg-info' -exec rm -rf {} +
	rm -rf build
	rm -rf .pytest_cache

dist: clean
	mkdir -p dist
	rm -f dist/amazon-notify.zip
	zip -r dist/amazon-notify.zip \
		amazon_notify \
		CHANGELOG.md \
		config.example.json \
		deployment \
		docs \
		LICENSE \
		Makefile \
		pyproject.toml \
		README.md \
		-x '*/__pycache__' \
		-x '*/__pycache__/*' \
		-x '*.pyc'
