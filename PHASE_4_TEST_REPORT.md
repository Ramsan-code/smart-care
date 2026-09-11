# Phase 4 implementation and verification

## Implemented

- Simulated hosted checkout creation with HMAC-signed success, failure, delayed and duplicate callbacks.
- Append-only charges and refunds with receipt numbers, payment history, payment-state synchronization and idempotent mutating API commands.
- Staff counter payments, partial/full refunds, cancellation and linked rescheduling.
- Same-fee moves commit immediately; higher-fee moves keep the original appointment until the additional payment succeeds; lower-fee moves create a traceable refund.
- Late, unmatched, malformed and amount-mismatched provider events create finance exceptions instead of confirming unavailable capacity.
- Facility-scoped finance access, exception assignment/resolution, and a migration for the finance app.

## API surface

`/api/v1/payments/checkouts/`, `/api/v1/payments/checkouts/<id>/simulate/`, `/api/v1/payments/callback/`, appointment payment/refund/history endpoints, cancellation and rescheduling endpoints, and finance-exception assignment/resolution endpoints are wired in `smartcare/urls.py`.

## Verification

Executed locally with a SQLite test backend because this workspace does not have the MariaDB client library or a running MariaDB service:

- Django system checks: passed.
- Migration consistency (`makemigrations --check --dry-run`): passed.
- Python compilation and Git whitespace checks: passed.
- Phase 4 signed adapter tests: 3 passed.
- Existing Phase 3 booking regression tests: 31 passed.
- Combined targeted run: 34 passed.

MariaDB-specific concurrency, callback persistence, and refund/reschedule integration tests still require the documented MariaDB test environment. The SQLite run is a regression and wiring check, not a claim of production database performance or locking behavior.
