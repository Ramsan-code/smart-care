# Phase 3 API

Use Django session authentication and CSRF for writes. Appointment and reservation identifiers are UUIDs. Times are ISO-8601 timestamps; fee values are decimal strings. Every hold, release and confirmation requires an `Idempotency-Key` header (1–100 characters). Reuse the same key and body after an uncertain outcome. Changing the body under an existing key returns 409.

## Availability

`GET /api/v1/availability/?facility_id=1&date=2026-09-14`

Optional filters: `doctor_id`, `service_id`, `specialty_id`, `page`. Omit `date` to search future available positions in the release window. Dated queries also label held/booked positions without exposing reservation owners. Responses include `results`, `count`, `next`, `previous`, `generated_at` and `facility_timezone`. Each result contains a slot UUID, local date, timestamps, version, state and fee preview. Page size is 100.

## Holds

`POST /api/v1/holds/`

```json
{"slot_id":"<slot UUID>","expected_version":1}
```

Reception must also include `patient_id`. Self-service uses the signed-in patient's profile and requires verified email. The patient and slot must belong to the same permitted facility. The response includes hold `id`, `version`, `expires_at`, `fee_preview` and `policy_version`.

`GET /api/v1/holds/<id>/` reads an owned hold.

`DELETE /api/v1/holds/<id>/` with `{"expected_version":1}` releases an owned held reservation. It cannot release a confirmed appointment.

## Confirmation

`POST /api/v1/appointments/`

```json
{"hold_id":"<hold UUID>","expected_version":1,"payment_method":"counter_due","consent_version":"demo-v1","reason_category":"new_visit"}
```

Reason categories: `new_visit`, `follow_up`, `routine`. Confirmation captures the user's explicit agreement to booking consent; reception records the patient's agreement. Responses contain the reference, confirmed state, counter-due payment state, saved fee/policy details and next action. Confirmation checks ownership, expiry, session activity and current occupancy again under lock. No arbitrary state/fee fields are accepted.

## History

`GET /api/v1/appointments/?q=SC-...&page=1` returns scoped history, 25 records per page. `GET /api/v1/appointments/<id>/` includes state history. Patients see their own bookings; reception and administrators see assigned facilities; doctors see only their own appointments at active memberships.

## Inline patients

Reception uses existing `GET /api/v1/patients/?q=...&facility_id=1` and `POST /api/v1/patients/`. Supply an idempotency key for creation retries. A new record has no login and returns `possible_duplicate` when a matching name exists at the facility. Contact results are masked. Existing account linkage is not automatic.

## Errors

Phase 3 domain APIs return `code`, `message`, `field_errors` and `correlation_id`: 400 invalid input/missing command key, 401 unauthenticated, 403 forbidden/unverified, 404 missing or out-of-scope resource, 409 stale version/capacity/state/idempotency conflict, 410 expired hold and 503 retryable database failure. Deadlock/lock-timeout commands retry up to three times with the same identity. Booking correctness depends on MariaDB, not Redis or browser polling.

## Operator commands

Publish a specific facility-local window:

```bash
.venv/bin/python manage.py generate_slots --start-date 2026-09-14 --days 30
```

Use `--facility <id>` to limit scope. Administrators can publish through `/schedule/publish/`. Existing published sessions are preserved. `/book/` contains both patient and reception flows; `/appointments/` contains scoped history. These shared routes replace the separate draft route names from the Phase 1 wireframes.
