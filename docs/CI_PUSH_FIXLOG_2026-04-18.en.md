# CI Fix Log (2026-04-18)

## Summary
Intermittent `lint` and `typecheck` failures on `push` CI were investigated and fixed.

## What Failed
- `CI / typecheck`
- `CI / lint` (`ruff format --check`)
- `CI / test` was skipped when upstream jobs failed

## Root Causes
1. `mypy` version drift between local and CI
- Local passed, but CI (`mypy 1.20.1`) raised `no-redef`/assignment typing errors.
- Target: `amazon_notify/gmail_client.py`

2. Pub/Sub import typing instability
- `from google.cloud import pubsub_v1` could trigger `attr-defined` in some environments.
- Target: `amazon_notify/streaming_pull.py`

3. Process gap before push
- A push happened before final `ruff format --check`, causing `lint` failure.

## Implemented Fixes
- `amazon_notify/streaming_pull.py`
  - Switched to `import google.cloud.pubsub_v1 as pubsub_v1` for stable type resolution.

- `amazon_notify/gmail_client.py`
  - Normalized request factory naming and fallback typing for CI mypy compatibility.
  - Restored `Request` compatibility alias used by existing tests.
  - Applied `ruff format` and verified clean `format --check`.

- Tests and coverage
  - Stabilized the flaky streaming test assertion that depended on logger capture side-effects.
  - Added unit coverage for:
    - `amazon_notify/commands/arguments.py`
    - `amazon_notify/commands/dispatch.py`
  - This recovered overall coverage to pass the CI threshold.

## Verification Commands
```bash
python -m ruff check amazon_notify tests
python -m ruff format --check amazon_notify tests
python -m mypy amazon_notify
pytest -q --cov=amazon_notify --cov-report=term-missing --cov-report=xml --cov-fail-under=90
```

## Prevention
- Always run the 4 commands above locally before pushing.
- If CI-only type errors appear, reproduce with a clean env (`venv` + `pip install -e .[dev]`) before patching.
