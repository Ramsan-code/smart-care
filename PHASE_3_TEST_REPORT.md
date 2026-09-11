# Phase 3 implementation and verification

Verified locally on 12 September 2026 using Python 3.12.3, Django 5.2.17, MariaDB 10.11.14, Redis 7.0.15 and Celery 5.6.3.

**84 automated tests passed in 7.327 seconds**, including 38 new Phase 3 tests and the 46 foundation tests. Django system checks, migration consistency, Python dependency checks, shell syntax and JavaScript syntax checks passed.

## Implemented

- Dated sessions and capacity-one slots generated from effective working hours, duration, breaks, buffer and leave. Publishing is repeatable and preserves existing sessions and appointments. Overlapping published sessions for the same doctor are rejected across services.
- Shared inventory for patient and reception booking, with doctor/session/slot locks, server-side release windows, stale-version checks, expiring holds and explicit hold release.
- Confirmation creates a unique reference, immutable fee/policy snapshot, consent record, appointment history, audit event and transactional outbox intent. Counter-due is the only payment method in this phase.
- Patient discovery by clinic, specialty, doctor, service and date, plus earliest available date search. History and confirmation details are scoped to the caller.
- Reception can search patient name, exact email or phone, create an unlinked patient, reserve/confirm a time and search a booking reference without leaving the booking screen. Duplicate names produce a warning and are never merged.
- Availability polling every 15 seconds, visible last-refresh status, stale-data warning, preserved form input and a hold countdown. The expiry worker runs every 30 seconds; booking also expires stale capacity synchronously.
- Administrator screen for publishing dated inventory; startup publishes the next 30 days from existing rules.

## Critical tests

Seven new TransactionTestCase scenarios ran against MariaDB with separate connections and synchronized competing requests:

1. Two patients competing for the same slot produce exactly one confirmed appointment.
2. Reception and a patient competing for the same slot produce one hold.
3. Duplicate concurrent confirmation commands produce one appointment, history record and outbox event.
4. Confirmation racing the expiry worker at the exact expiry boundary cannot create an appointment.
5. Concurrent generation of the same session creates one session.
6. Confirmation racing hold release leaves either a confirmed occupied slot or a cancelled released hold, with consistent inventory.
7. Concurrent confirmations with different idempotency keys still create only one appointment and history record.

Other regression coverage includes break/leave/effective-date rules, buffers, published overlap, snapshot immutability, fee changes after a hold, expiry without a worker, repeated expiry/release (including release after the worker advances the hold version), blocked/past/inactive sessions, missing keys and stale versions, unsupported payment methods, consent, other-patient access, revoked reception membership, doctor scope, inline patient creation retries, rollback on outbox failure, CSRF, earliest availability and booking-reference lookup.

## Browser and runtime evidence

- Patient demo account reserved and confirmed SC-67CD2E1F2A2C4DDAA844 at LKR 2,500, then opened its saved confirmation.
- Reception saw that same slot as booked, searched an existing patient, reserved/released another time, created the synthetic “Phase 3 Demo Walk-in” patient and confirmed its booking in the same screen.
- Booking-reference lookup displayed the patient's existing confirmation inline. Earliest-date lookup returned a published available date.
- Desktop layout visually inspected. Booking layout checked at 768 and 360 pixels with no horizontal overflow. Patient confirmation visually inspected at 360 pixels.
- Startup generated 36 sessions / 396 slots from the existing demo rules and preserved existing users. The running worker processed the patient confirmation outbox event; no confirmation event remained pending at that check.
- Automated tests used a separate test database. Two synthetic browser-test confirmations and the walk-in patient remain available in the local demo database.

## Boundaries

Online payments, cash recording/receipts, cancellation and rescheduling belong to Phase 4. Queue transitions, disruptions and notification delivery belong to Phase 5. The confirmation outbox consumer records an acknowledgment; it does not send a message. Published sessions are preserved; changing rules does not silently rewrite an existing session.

The full T06 payment matrix is not claimed complete: this phase verifies counter-due booking. Remote CI, a fresh-machine install, a complete accessibility audit, 200% zoom, and the Phase 7 load/recovery test have not been executed for this change.

## Follow-up recheck

The full suite was rerun on 12 September 2026: 84 tests passed. No additional application defect was found. System/migration/dependency/JavaScript checks and Git whitespace validation passed. Live checks found two confirmed demo bookings, zero booked reservations missing an active slot, zero mismatched active slot pointers, and zero pending confirmation events. Redis write/read succeeded. Browser reference lookup still returned the correct saved booking, the narrow viewport had no horizontal overflow (measured content width 327 pixels), and no browser console errors were recorded. Live user data was preserved; new race tests ran only in the isolated test database.
