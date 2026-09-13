# Booking-to-settlement integrity handoff

## Baseline and scope

Starting commit: `aaa323efe7b1a5c58a3ac55ef85851ec0d9f6054`.
Current branch: `fix/booking-settlement-integrity`. Implementation is uncommitted.
The seven previously modified files were preserved and extended. No reset, clean,
automatic stash, commit, push or live-provider submission was performed. No applicable
AGENTS.md was found in the repository or its ancestor directories.

The earlier branch error was:

```text
fatal: cannot lock ref 'refs/heads/fix/transaction-boundaries': unable to create directory for .git/refs/heads/fix/transaction-boundaries
```

The earlier filesystem sandbox prevented Git metadata writes. Once filesystem access
was available, `git switch -c fix/booking-settlement-integrity` succeeded. The branch
restriction is resolved.

The prior simulation, multipart upload and SMS transaction fixes are retained. Their
tests were rerun on MariaDB, including the SMS TransactionTestCase that was previously
unexecuted. One synthetic callback test fixture was changed to a valid UUID because
callback validation now validates the actual checkout identifier format.

## Implemented controls

### Settlements

A nullable unique `SettlementLine.original_payable` identifies the original allocation.
Refund lines leave it NULL, and instead require an original line and a unique supporting
refund. A database check constrains the two line shapes. This uses ordinary unique
indexes and a check constraint, not a conditional unique constraint.

A dedicated `SettlementMutex` row serializes settlement changes within each facility.
It avoids locking the Facility row, whose foreign-key locks also affect booking.
Eligible payable rows are locked in primary-key order. Original allocation uniqueness
still protects the database independently of service locking.

The facility/reference identity is replayable; reuse with a different creator, currency
or adjustment is rejected. HTTP create, transition and refund-adjustment commands also
require `Idempotency-Key`, preserve the original response, and reject conflicting reuse.
Batch creation selects only the requested currency. Unbacked batch adjustments are
rejected; corrections require a linked refund. Display and CSV use decimal amounts,
and the export route precedes the generic transition route.

A creator cannot approve their own settlement, including administrators. An independent
administrator or facility finance approver can approve. Facility membership applies to
non-superuser administrators as well as other financial staff. Paid batches and all
existing lines reject ORM edits/deletion; new lines require a draft batch. Paying a
refund-adjustment batch does not mutate the original payable's paid timestamp.

### Refund adjustments

The supplied original batch must be paid and must contain the payable's original line.
The supporting payment must be a successful refund for the same appointment, facility
and currency. Each refund can support only one adjustment, enforced by a unique index.

Allocation policy: the maximum doctor adjustment for each refund is
`floor_to_cents(refund.amount * payable.amount / appointment.snapshot.total)`.
The stored doctor fee must be present, positive, no greater than the stored total, and
at least the payable amount. Thus a patient refund is not assumed to belong wholly to
the doctor. Rounding is conservative per refund. Unused eligible amounts from a refund
are not automatically reassigned or applied a second time.

Payable locking and the facility settlement mutex protect cumulative limits. Total
negative adjustments cannot exceed the payable, even when legacy supporting refunds
already exceed the original collection. Corrections are linked new batches and lines;
the paid original remains unchanged.

### Refund execution and transferred credit

Refund commands reserve the balance and persist one operation per collected funding
source, together with committed outbox work. The initial response is pending and includes
`obligation_id` plus `obligation_ids`; it does not claim a successful refund.
Multiple collections are split into separate durable operations, each bounded by its
source balance.

Financial availability subtracts pending, submitting and unknown operations and outgoing
credit. Incoming transferred credit follows the immutable replacement chain back to the
actual collected payments. Cancellation of a credited replacement can therefore refund
the original collection without making it refundable twice. Payable creation also uses
those funding sources and requires the stored doctor-fee snapshot.

Workers claim a short lease, release the database transaction, submit using
`refund:<committed-operation-UUID>`, and then finalize under locks. Success requires a
successful provider outcome and a usable provider reference. Pending, failed and unknown
outcomes are distinct; timeout is unknown. Unknown operations retain their reservation.
A stale attempt cannot overwrite a newer lease's result. Nullable unique payment
relationships enforce one local ledger entry per refund operation and checkout.

Automatic retries are bounded. Exhausted uncertain operations remain owned by finance
with their funds reserved. An operator can requeue the original identity:

```bash
.venv/bin/python manage.py retry_refund OPERATION_UUID
```

This does not reset the operation identity, release reserved funds, retry a confirmed
failure, or bypass an active worker lease. Legacy operations without a proven source
identity require manual provider reconciliation and cannot use this command.

### Booking, notifications and callbacks

Booking resource locks follow doctor IDs, session IDs, then slot IDs in sorted order
before dependent reservation/appointment/change rows. Session cancellation starts with
the doctor lock, locks slots before appointments, and clears outstanding holds.
Rescheduling locks both slot hierarchies before locking the original appointment.
Cancellation-resolution versions are checked after the resolution lock.

Locking queries avoid incidental `select_related` locks on unrelated rows. SMS paths
lock the appointment before its delivery and check retry eligibility under the lock.
Refund finalization uses the appointment booking hierarchy before the operation.
A command retries only MariaDB 1205/1213 failures, with a three-attempt bound.

Notification identity comes from the committed outbox key, not the appointment's current
version. The rendered body is saved on the delivery. Confirmation, change and reminder
messages are suppressed for cancelled, rescheduled, completed, no-show or left
appointments. Legacy delivery bodies are frozen at their first subsequent attempt.
Real SMS provider guarantees are not established; no exactly-once external-delivery
claim is made.

Authenticated checkout callbacks validate payload shape, amount, UUID and provider
reference. A successful checkout is not downgraded by a later failure; duplicate
callbacks cannot insert another payment. Conflicting reuse of a recorded payment event
identity is rejected. Malformed signed callback identities are derived from a body hash
rather than arbitrary supplied values.

Outbox failures retain unprocessed work, record ownership and exponential retry delay,
and still raise the original consumer exception for worker visibility. After eight
failures they enter a dead-letter state. Dispatch orders by availability and ID, excluding
dead-lettered work. Refund provider I/O occurs outside the outbox transaction too.

### Production behavior

Only simulated and local failure-harness payment adapters are implemented. Both require
DEBUG and DEMO_MODE. Production checks fail clearly with `finance.E001`, and the adapter
factory rejects simulation even if the URL configuration is bypassed. SMS simulation
is also guarded. A real production provider and its reconciliation/callback contract
must be implemented before production deployment.

## Migrations and existing data

- `communications/0002_delivery_body`: persisted notification body.
- `core/0002_outboxevent_attempts_outboxevent_dead_lettered_at_and_more`: retry ownership,
  delay accounting and dead-letter state.
- `finance/0004_refundobligation_actor_refundobligation_attempts_and_more`: durable refund
  fields, explicit original/adjustment relationships and line-shape check.
- `finance/0005_settlementmutex_payment_refund_operation_one_ledger_and_more`: facility
  mutex and unique local ledger relationships.

Finance preflights run before schema-changing operations because MariaDB DDL is not
transactionally reversible. Duplicate/unlinked/negative legacy settlement lines and
unbacked batch adjustments cause an actionable failure with record IDs. Duplicate
checkout/refund ledger relationships also stop migration. No financial records are
silently deleted. Unambiguous original lines are backfilled; old pending refunds become
unknown, finance-owned records without automatic resubmission.

The migration regression creates a conflicting legacy allocation, proves both records
remain after failure and that new columns were not added, then removes only the
synthetic test conflict and verifies successful backfill.

The pre-existing local SQLite file was inspected read-only: it contained zero settlement
batches, settlement lines and refund obligations. It was not converted, modified or used
to validate locking. The new project-local MariaDB application database received schema
migrations; all fixtures, recovery and load data were created in `test_smartcare`.
Existing SQLite accounts/data have not been imported into MariaDB.

## Reproduce validation

Use Python 3.12 on Ubuntu 24.04. Setup downloads and extracts local MariaDB/Redis packages
and installs the pinned Python dependencies; it does not require system service changes.

```bash
cd /path/to/smart-care
bash scripts/setup_local.sh
.venv/bin/python scripts/run_local.py --services-only
```

Keep services running. In another terminal, run the following sequentially. Tests,
recovery and load all own `test_smartcare`; never run them concurrently or point an
application worker at that database.

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py migrate --noinput
.venv/bin/python -m pip check
.venv/bin/python scripts/check_production.py
.venv/bin/python manage.py test --debug-mode --noinput
.venv/bin/python scripts/recovery_harness.py
.venv/bin/python scripts/load_test.py --seconds 60 --users 8 --output .runtime/load-results.json
git diff --check
```

The test runner deliberately uses `--debug-mode` for the demo-only payment configuration.
Production rejection is tested separately; production checks are not silenced.

Recovery launches its own loopback Redis, HTTP provider, Celery worker and beat.
The provider stores accepted operation keys in an independent SQLite ledger; this
SQLite file is a provider test double, not the application's transaction database.
The worker is SIGKILLed after acceptance. Recovery then restarts the worker, stops and
restarts Redis with persistence, and proves committed outbox work drains. Reports and
process logs are retained under `.runtime/recovery-*/`.

## Observed results

Environment: Python 3.12.3; Django 5.2.17; MariaDB 10.11.14/InnoDB; mysqlclient 2.2.8;
Redis server 7.0.15; redis-py 6.4.0; Celery 5.6.3. The local database is the version
provided by the supported Ubuntu setup script. CI specifies MariaDB 11.4.

Passed:

- 123 tests on MariaDB in 17.619 seconds, including separate-connection races.
- System checks, migration consistency, migrations, dependency checks and diff checks.
- Production fail-closed check.
- Real worker recovery: two provider submissions, one accepted operation, one refund
  ledger entry after worker termination and restart.
- Redis interruption/restoration: 25 committed backlog events recovered; poison work
  deferred; original settlement replay produced one allocation.
- HTTP smoke load: 8 concurrent authenticated patients, 60 seconds, 396 available slots,
  344 measured requests, 5.733 requests/second, no unexpected HTTP/transport errors,
  zero checked booking invariant violations, eight confirmations, no pending outbox
  events at completion. Availability p50/p95: 211.43/303.59 ms; hold: 302.68/422.70 ms;
  confirmation: 465.14/499.32 ms. Workload uses two-second closed-loop patient cycles
  over loopback Django's development server. This is not sustained production capacity
  evidence or a financial-throughput benchmark.

Failed/external:

- The original GitHub job did not start. GitHub's check annotation for run
  [34655128537](https://github.com/Ramsan-code/smart-care/actions/runs/34655128537) says:
  “The job was not started because your account is locked due to a billing issue.”
  This is an account/infrastructure failure, not a failing application assertion.
- Normal checks with production/demo-disabled settings intentionally fail with
  `finance.E001`; the dedicated guard test verifies that expected failure.
- Intermediate implementation runs exposed test failures (callback fixture format,
  error propagation, decimal response formatting). These were repaired; the final
  test run has no failures.

Unexecuted/remaining limits:

- Real payment or SMS sandbox validation: no provider selection/credentials or actual
  provider idempotency, timeout and refund callback contract are available.
- Remote CI and MariaDB 11.4 validation: workflow changes are unpushed; the observed
  GitHub account billing block must be resolved before a remote run can start.
- Sustained production load, real network/provider latency and long-outage soak tests.
- Historical pending refunds and ambiguous legacy settlement data require explicit
  finance reconciliation; the migration intentionally does not guess relationships.
- Direct DBA SQL is outside the ORM immutability controls. Payment/settlement allocation
  uniqueness and line shapes are database-enforced; paid-history update prevention
  also relies on application/ORM controls.
- Notification external I/O still occurs inside a transaction. The local provider is
  deterministic, but real-provider reconciliation and delivery guarantees remain work
  for a production integration.

## Invariant-to-test mapping

All named test cases below passed in the final MariaDB run.

- Last-slot capacity, confirmation replay, expiry/release races:
  `appointments.tests.BookingConcurrencyTests`.
- Original allocation race and database uniqueness:
  `FinanceIntegrityTests.test_concurrent_batches_allocate_payable_once`,
  `test_database_rejects_duplicate_original_allocation`.
- Settlement response replay/conflicting identity and exports:
  `test_settlement_replay_and_conflict`, `test_settlement_api_idempotency_and_export`,
  `test_settlement_transition_api_replay`.
- Paid-batch membership/supporting refund:
  `test_adjustment_requires_paid_batch_membership_and_refund`,
  `test_adjustment_api_rejects_missing_refund_and_foreign_finance`.
- Doctor share, immutable paid records, adjustment replay/cumulative races:
  `test_refund_share_replay_and_immutable_paid_records`,
  `test_concurrent_refund_adjustment_replay_allocates_once`,
  `test_concurrent_adjustments_cannot_exceed_payable_even_with_bad_legacy_refunds`.
- Refund reservation and rollback:
  `test_concurrent_refunds_reserve_balance`,
  `test_refund_replay_and_rollback_has_no_provider_effect`.
- Provider succeeded/failed/pending/unknown/timeout, deduplication, transaction boundary:
  `test_provider_outcomes_and_duplicate_processing`,
  `test_provider_call_runs_outside_database_transaction`,
  `test_refund_operator_retry_preserves_identity_and_reservation`.
- Transferred credit and multiple collections:
  `test_transferred_credit_can_be_refunded_once_from_original_collection`,
  `test_refund_is_split_into_durable_operations_for_multiple_collections`.
- Duplicate/out-of-order signed callbacks:
  `test_duplicate_and_out_of_order_callbacks` and `finance.tests.PaymentAdapterTests`.
- Cross-facility controls/self-approval:
  `test_self_approval_and_cross_facility_denied`,
  `test_adjustment_api_rejects_missing_refund_and_foreign_finance`.
- Production simulation rejection and real multipart boundary:
  `finance.tests.PaymentSafetyTests`, `test_production_configuration_fails_clearly`.
- Session cancellation race and resolution locking:
  `test_booking_confirmation_races_session_cancellation`,
  `test_resolution_version_is_checked_under_lock`.
- SMS transaction boundary, concurrent retries, stable identity and suppression:
  `communications.tests.DeliveryTransactionTests`.
- Poison isolation/quarantine:
  `test_outbox_poison_does_not_block_valid_work`,
  `test_unknown_outbox_event_is_quarantined_after_retry_budget`.
- Safe legacy migration:
  `test_migration_preflight_preserves_conflicting_legacy_allocations`.
- Actual crash/redelivery/broker restoration: `scripts/recovery_harness.py`, with real
  worker/beat processes and the independent provider ledger.

## Changed files

- `appointments/services.py`: ordered booking resource locks; recognized transient retries only.
- `appointments/operations.py`: session cancellation lock order, hold release, scoped administration,
  locked resolution version checks and reserved refund balances.
- `communications/adapters.py`: reject production simulation.
- `communications/models.py`: frozen delivery body.
- `communications/services.py`: stable event identity, atomic SMS retry, suppression and retry limit.
- `communications/tests.py`: database and concurrency regression coverage.
- `communications/migrations/0002_delivery_body.py`: body schema.
- `core/models.py`: outbox retry/dead-letter metadata.
- `core/tasks.py`: bounded recovery, poison isolation and refund dispatch outside the outbox transaction.
- `core/migrations/0002_outboxevent_attempts_outboxevent_dead_lettered_at_and_more.py`: outbox schema.
- `finance/models.py`: durable refund fields, allocation/ledger uniqueness and immutable settlement controls.
- `finance/phase6.py`: settlement locking, source/currency controls, linked proportional adjustments and exports.
- `finance/services.py`: durable refund intents, funding-chain accounting and callback integrity.
- `finance/refunds.py`: leased provider submission and reconciliation.
- `finance/adapters.py`: guarded adapter selection, callback validation, durable local test adapter.
- `finance/apps.py`: fail-closed production configuration checks.
- `finance/api.py`: multipart file validation, supporting refund field and idempotent settlement commands.
- `finance/tests.py`: production/multipart coverage and valid callback fixture.
- `finance/test_integrity.py`: financial, API, concurrency, migration and recovery-service tests.
- `finance/management/commands/retry_refund.py`: audited operator requeue of the original identity.
- `finance/management/__init__.py`, `finance/management/commands/__init__.py`: command package markers.
- `finance/migrations/0004_refundobligation_actor_refundobligation_attempts_and_more.py`: preflight, backfill and durable relationships.
- `finance/migrations/0005_settlementmutex_payment_refund_operation_one_ledger_and_more.py`: mutex and ledger constraints with preflight.
- `smartcare/settings.py`: explicit demo provider, leases/retry settings, broker redelivery and recovery interval.
- `smartcare/urls.py`: guarded simulation route and correct export routing.
- `scripts/fake_payment_provider.py`: separate accepted-operation ledger and failure injection.
- `scripts/recovery_harness.py`: isolated real-worker and broker interruption checks.
- `scripts/check_production.py`: executable production safety assertion.
- `scripts/load_test.py`: require MariaDB, report throughput and accurate provider scope.
- `.github/workflows/checks.yml`: local binary setup, explicit demo tests, production check,
  real recovery harness and retained process evidence.
- `README.md`, `docs/INTEGRITY.md`: corrected execution path, changed contracts and reproducible handoff.

## PR-ready description

Prevent duplicate settlement allocation and false refund success under retries. Original
allocations and refund adjustments now have explicit, database-enforced identities;
adjustments require a paid source and eligible supporting refund, and approval requires
a different actor. Refund requests persist reserved operations and outbox work before
provider submission, including transferred credit and multiple source collections.

Also repair booking/session lock order, notification retry identity, cancellation version
checks, production simulation guards and settlement exports. Validation includes 123
MariaDB tests, safe-migration preflight, real worker/broker recovery and a one-minute
HTTP smoke load. Apply migrations after resolving reported legacy conflicts, update
clients for pending refund operation IDs and settlement idempotency headers, and keep
production disabled until a real provider contract is implemented and tested.
