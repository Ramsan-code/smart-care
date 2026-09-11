# Phase 7 load and recovery validation

Phase 7 makes the production correctness checks repeatable and explicitly
tagged as `phase7`.

## Validation commands

Run the deterministic recovery checks locally:

```bash
DB_ENGINE=sqlite DJANGO_DEBUG=true DJANGO_SECRET_KEY=test DB_PASSWORD=test \
  .venv/bin/python manage.py test core.tests.FoundationTests --tag phase7
```

Run the MariaDB concurrency/load checks in the CI-style environment:

```bash
.venv/bin/python manage.py test --tag phase7 --noinput
```

The tagged checks cover:

- dispatching only due, unprocessed outbox events;
- retaining unsupported/failed events for later recovery;
- idempotent concurrent commands;
- last-slot booking races;
- confirmation/release and expiry/confirmation races;
- concurrent session generation without duplicate inventory.

SQLite can run the recovery checks, but it cannot validate the concurrency
guarantees: its table-level locking produces `database table is locked` under
the threaded tests. MariaDB with InnoDB row locking is required for the
concurrency/load portion. Redis/Celery worker interruption and redelivery
remain environment-level checks covered by the existing late-ack and beat
configuration.
