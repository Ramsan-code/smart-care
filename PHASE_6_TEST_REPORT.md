# Phase 6 test report

Phase 6 adds gateway reconciliation, doctor payable generation, settlement
batches, exports, doctor statements, operational finance reporting, and
post-settlement refund adjustments.

Validated locally with SQLite:

- `manage.py check`
- `manage.py makemigrations --check --dry-run`
- Python compilation and `git diff --check`
- 41 targeted finance, communications, appointments, and core tests

The local suite passed. MariaDB concurrency and row-lock behavior still
requires the project’s intended MariaDB environment; SQLite cannot validate
those guarantees and may report table-lock errors in the existing concurrency
tests.
