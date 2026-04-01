.PHONY: setup setup-dev test lint run-once run reauth clean dist

setup:
	python3 -m venv .venv
	. .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt

setup-dev:
	python3 -m venv .venv
	. .venv/bin/activate && pip install --upgrade pip && pip install -r requirements-dev.txt

test:
	. .venv/bin/activate && pytest -q

lint:
	. .venv/bin/activate && python -m py_compile amazon_mail_notifier.py

run-once:
	. .venv/bin/activate && python amazon_mail_notifier.py --once

run:
	. .venv/bin/activate && python amazon_mail_notifier.py

reauth:
	. .venv/bin/activate && python amazon_mail_notifier.py --reauth

clean:
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
	rm -rf .pytest_cache

dist: clean
	mkdir -p dist
	zip -r dist/amazon-notify-v2.zip . \
		-x '.git/*' \
		-x '.venv/*' \
		-x 'dist/*' \
		-x '*.pyc' \
		-x '*/__pycache__/*' \
		-x '.pytest_cache/*' \
		-x 'config.json' \
		-x 'credentials.json' \
		-x 'token.json' \
		-x 'state.json' \
		-x 'logs/*'
