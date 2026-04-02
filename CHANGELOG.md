# Changelog

All notable changes to this project will be documented in this file.

## [0.1.3] - 2026-04-03

### Added
- Added CLI operational commands: `--dry-run`, `--test-discord`, `--validate-config`, and `--health-check` (JSON output).
- Added test coverage execution via `pytest-cov`, `make coverage`, and CI coverage reporting.

### Changed
- Improved automated test coverage to 80%+ (current total: 88% with `pytest --cov`).
- Added Ruff linting and mypy type checking to the CI pipeline.
- Improved retry/error-handling internals (`refresh_with_retry`, transient error chain depth guard).
- Improved notifier observability by adding non-Amazon skip counts to completion logs.
- Tightened type hints around runtime state file handling.

## [0.1.1] - 2026-04-02

### Changed
- Unified the public name to `amazon-notify` across the README, systemd sample, package metadata, and distribution archive.
- Renamed the distribution archive to `dist/amazon-notify.zip`.
- Added a notification example and tighter release-facing documentation.

## [0.1.0] - 2026-04-02

### Added
- Initial public release of the Gmail-to-Discord Amazon mail notifier.
- Gmail polling, Amazon mail filtering, Discord notifications, and state-based duplicate suppression.
- Token error alerts, transient failure recovery notifications, tests, CI, and systemd deployment sample.
