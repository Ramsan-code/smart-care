# Smart Care — Phase 3

Django foundation for the Smart Care clinic demo: accounts and verification, consent history, facility-scoped roles, configuration, immutable effective-dated fees and policies, audit history, and a transactional outbox. Phase 3 adds shared scheduling, expiring holds, counter-due bookings and appointment history. Payments and appointment changes belong to Phase 4.

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

Tests create and destroy a separate `test_smartcare` database. The live application database is preserved. See `PHASE_3_TEST_REPORT.md` for the latest verified results and limits; `PHASE_2_TEST_REPORT.md` records the foundation baseline.

The GitHub Actions workflow specifies Python 3.12, MariaDB 11.4 and Redis 7. It requires a repository push to execute and has not been run remotely.

## Use Phase 3

- Patients: sign in, open **Book a visit**, choose an available time, accept the displayed consent and confirm **Pay at counter**. Verify your email through the demo inbox first if required.
- Reception: open **Book a visit**, select or create a patient, then reserve and confirm. Patient lookup and booking-reference lookup are available on the same screen.
- Administrators: use **Publish slots** after configuring working hours, fees, policies and leave. Startup also publishes a rolling 30-day window, preserving existing sessions and appointments.
- **Appointments** shows permitted booking history and saved fee details.

Availability refreshes every 15 seconds; holds expire after the configured interval (five minutes by default). The database rechecks every booking, so an old browser view cannot double-book a slot. Confirmation outbox events are acknowledged locally; no real notification or payment occurs.

API contracts and generation commands: `PHASE_3_API.md`.
