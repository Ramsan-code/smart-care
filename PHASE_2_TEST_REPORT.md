# Phase 2 verification — 12 September 2026

**Result: 46 automated tests passed in 10.880 seconds.**

Tested locally with Python 3.12.3, Django 5.2.17, MariaDB 10.11.14 and Redis 7.0.15. Tests used an isolated database; the live database and existing user accounts were preserved.

## Coverage and checks

- Authentication, verification, password reset, consent, CSRF and login throttling.
- Role/facility isolation, patient contact masking, privilege escalation rejection and membership revocation.
- Configuration validation, effective-dated fees and policies, and scoped administration.
- Append-only audit, transactional outbox rollback/deduplication and concurrent idempotent execution.
- Django system check: no issues. Migration check: no changes detected. Python dependencies: no broken requirements. Startup shell scripts: syntax passed.
- Browser: administrator sign-in and navigation, desktop dashboard and 360-pixel mobile layout. Homepage has no horizontal overflow or overlapping team/clinic labels at 360 pixels after correction.
- Live Redis cache write/read returned the expected value. Celery worker responded to ping. A unique demo.ping outbox event was processed by the running worker and recorded once in ProcessedEvent.

## Corrections made during verification

- Explicitly selected the local inbox backend in email tests, because Django's test runner otherwise substitutes its in-memory email backend.
- Guarded missing dates/times so incomplete configuration forms return validation errors rather than raising comparison/query exceptions.
- Enforced immutable policy versions, rejected overlapping intervals, and closed preceding open intervals when a later policy is published.
- Adjusted mobile illustration labels and versioned the stylesheet URL to invalidate stale browser CSS.
- Improved startup process checks to avoid treating unrelated processes with reused PIDs as workers; added supervised shutdown.

## Limits

The GitHub Actions workflow has not run remotely. A fresh-machine bootstrap, full accessibility audit, load test and penetration test have not been performed. MariaDB 11.4 is configured in CI but was not the local test database version. Booking, payments and real notification delivery are outside Phase 2.
