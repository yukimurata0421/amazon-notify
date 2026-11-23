# Operational Evidence (November 23, 2026)

This snapshot summarizes the retained execution history of one self-hosted
Amazon Notify installation as of November 23, 2026, 21:49 JST. The counts
describe the installation's run history. Per-run release attribution is
unavailable.

| Measure | Observed result |
|---|---:|
| Timestamped history with a known time zone | 232 days, 4 hours, 54 minutes |
| Recorded runs | 84,229 |
| Successful runs | 84,219 |
| Failed runs | 10 |
| Run-level success rate | 99.988% |
| Delivery notifications counted as handled by the application | 148 |
| State-consistency checks passed | 10 of 10 |

## Measurement

- The earliest run with a UTC offset started on April 5, 2026, at 16:55:25 JST.
  The last captured run started on November 23, 2026, at 21:49:25 JST and ended
  at 21:49:26 JST. One earlier run is included in the counts. Its recorded
  start is April 4, 2026, at 03:58:09 with no time zone, so the duration starts
  at the first run with a known offset.
- Run counts come from the retained `runs.jsonl`. A run is counted as successful
  when `failure_kind` is null or empty. This includes runs with no matching
  mail. All 10 recorded failures were classified as `source_failed`.
- The notification count is the sum of `notified_count`. The code can count
  deduplicated notifications and dry-run sends as handled. Discord-side
  receipt and end-to-end delivery availability were outside this measurement.
- `amazon-notify --verify-state` passed at 21:44 JST on the same date. It checked
  state and ledger readability, checkpoint and run-summary consistency,
  checkpoint timestamp ordering, and incident lifecycle consistency.

## Recovery and uptime

The last recorded incident closed on August 18, 2026, at 18:06:25 JST,
following a successful run. The retained history contains no failed runs in
the approximately 97 days between that recovery and this snapshot. That
interval measures recorded run outcomes. Process uptime and time without
operator intervention require separate evidence.

The latest documented manual recovery is the May 4 incident. The operator
sent a termination signal and systemd brought the service back at 23:01:12 JST.
The systemd journal also shows a service restart on November 14, 2026, at
15:56:22 JST. The initiator and purpose of that restart are unconfirmed.

[May 4 incident report](./ops/INCIDENT-2026-05-04-SSL-HANDSHAKE.md)
