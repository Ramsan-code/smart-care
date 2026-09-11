# Smart Care — Phase 2

Django foundation for the Smart Care clinic demo: accounts and verification, consent history, facility-scoped roles, configuration, immutable effective-dated fees and policies, audit history, and a transactional outbox. Booking and payments belong to later phases.

## Run this workspace

```bash
cd /home/ramsan/Documents/Codex/2026-09-11/cr/outputs/smart-care
bash start.sh
```

Open http://127.0.0.1:8000/ and keep the terminal running. Ctrl+C stops the supervised application processes; database data persists. If the browser reports connection refused, start the command above and check its terminal output.

Local account passwords are in `demo-credentials.txt`. The administrator email is `admin@example.test`. Existing accounts are preserved by startup seeding. Use the administrator workspace to open Configuration, Team and Audit. Patient accounts have their own profile and demo inbox. Email is captured locally; this demo does not send real mail.

## Setup on another machine

The helper targets Ubuntu 24.04 x86_64 with Python 3.12, venv support, a C compiler and development headers, apt download access, and internet access for Python dependencies:

```bash
bash scripts/setup_local.sh
bash start.sh
```

It extracts local MariaDB/Redis binaries without sudo, creates `.venv` and generates `.env` secrets. Database port is 3307, Redis 6380, and Django 8000. The setup script has been syntax checked; a fresh-machine installation has not been tested. Do not publish `.env`, `demo-credentials.txt` or `.runtime`.

## Validate

With local services running:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test --noinput
.venv/bin/python -m pip check
```

Tests create and destroy a separate `test_smartcare` database. The live application database is preserved. See `PHASE_2_TEST_REPORT.md` for verified results and limits.

The GitHub Actions workflow specifies Python 3.12, MariaDB 11.4 and Redis 7. It requires a repository push to execute and has not been run remotely.
# smart-care
