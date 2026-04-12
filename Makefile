.PHONY: setup setup-dev test coverage lint ruff format-check typecheck release-check run-once run reauth dry-run test-discord validate-config health-check install-systemd clean dist

VENV_BIN = .venv/bin
PYTHON = $(VENV_BIN)/python
PIP = $(VENV_BIN)/pip
CLI = $(VENV_BIN)/amazon-notify
RUFF = $(VENV_BIN)/ruff
MYPY = $(VENV_BIN)/mypy
VERSION = $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)

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
	$(PYTHON) -m pytest -q --cov=amazon_notify --cov-report=term-missing --cov-report=xml --cov-fail-under=90

lint:
	$(PYTHON) -m compileall -q amazon_notify
	$(RUFF) format --check amazon_notify tests
	$(RUFF) check amazon_notify tests

ruff:
	$(RUFF) check amazon_notify tests

format-check:
	$(RUFF) format --check amazon_notify tests

typecheck:
	$(MYPY) amazon_notify

release-check:
	$(RUFF) check amazon_notify tests
	$(RUFF) format --check amazon_notify tests
	$(MYPY) amazon_notify
	$(PYTHON) -m pytest -q --cov=amazon_notify --cov-report=term-missing --cov-report=xml --cov-fail-under=90
	docker build -t amazon-notify:$(VERSION) .
	docker run --rm -v "$(CURDIR):/work" amazon-notify:$(VERSION) --config /work/config.example.json --validate-config

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

install-systemd:
	bash deployment/systemd/install-systemd.sh --mode hybrid --no-install-deps

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
		README.ja.md \
		-x '*/__pycache__' \
		-x '*/__pycache__/*' \
		-x '*.pyc'
