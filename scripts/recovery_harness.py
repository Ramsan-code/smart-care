"""Real worker/broker recovery against an isolated MariaDB test database.

Run after normal tests; never run concurrently with another test database user.
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


broker_port, provider_port = free_port(), free_port()
os.environ.update(DJANGO_SETTINGS_MODULE='smartcare.settings', DJANGO_DEBUG='true', DEMO_MODE='true',
    PAYMENT_PROVIDER='test_http', TEST_PAYMENT_URL=f'http://127.0.0.1:{provider_port}',
    REDIS_URL=f'redis://127.0.0.1:{broker_port}/0',
    REFUND_LEASE_SECONDS='2', CELERY_VISIBILITY_TIMEOUT='2', OUTBOX_RECOVERY_SECONDS='1')
import django
django.setup()
from django.conf import settings
from django.db import connection
from django.test.runner import DiscoverRunner
from django.utils import timezone
from finance.test_integrity import FinanceIntegrityTests
from finance.models import RefundObligation, Payment, SettlementLine
from core.models import OutboxEvent
from core.tasks import process_event
from finance.phase6 import create_settlement

if connection.vendor != 'mysql':
    raise SystemExit('MariaDB/InnoDB is required.')

run_dir = Path(tempfile.mkdtemp(prefix='recovery-', dir=ROOT / '.runtime'))
processes = []
report = {'checks': {}, 'run_directory': str(run_dir)}


def wait_for(predicate, description, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.2)
    raise AssertionError(f'Timeout: {description}')


def spawn(name, argv):
    log = (run_dir / f'{name}.log').open('ab')
    proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
    processes.append(proc)
    return proc


def stop(proc, hard=False):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def provider(path='/', data=None):
    request = Request(settings.TEST_PAYMENT_URL + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=2) as response:
        return json.load(response)


runner = DiscoverRunner(verbosity=0, interactive=False)
old_config = runner.setup_databases()
env = dict(os.environ, DB_NAME=connection.settings_dict['NAME'])
env['LD_LIBRARY_PATH'] = str(ROOT / '.runtime/services/usr/lib/x86_64-linux-gnu')
redis_binary = os.getenv('RECOVERY_REDIS_BINARY', str(ROOT / '.runtime/services/usr/bin/redis-server'))
if not Path(redis_binary).exists():
    import shutil
    redis_binary = shutil.which('redis-server')
if not redis_binary:
    runner.teardown_databases(old_config)
    raise SystemExit('Redis binary required; run local setup or set RECOVERY_REDIS_BINARY.')
redis_args = [redis_binary, '--bind', '127.0.0.1', '--port', str(broker_port),
    '--dir', str(run_dir), '--appendonly', 'yes', '--appendfsync', 'always', '--save', '']
worker_args = [sys.executable, '-m', 'celery', '-A', 'smartcare', 'worker', '--pool=solo',
    '--loglevel=INFO', '--hostname=recovery@%h']
try:
    fixture = FinanceIntegrityTests()
    fixture.setUp()
    operation_data = fixture.request_refund('500.00', key='worker-death')
    fixture.doCleanups()  # Worker uses wall time, not the fixture's booking clock.
    RefundObligation.objects.filter(pk=operation_data['obligation_id']).update(next_attempt_at=timezone.now())
    OutboxEvent.objects.all().update(available_at=timezone.now())
    with connection.cursor() as cursor:
        cursor.execute('SELECT VERSION(), @@default_storage_engine')
        report['database'] = list(cursor.fetchone())
    report['python'] = sys.version.split()[0]
    redis_proc = spawn('redis', redis_args)
    import redis
    broker = redis.Redis(host='127.0.0.1', port=broker_port)
    def ready():
        try:
            return broker.ping()
        except redis.ConnectionError:
            return False
    wait_for(ready, 'broker ready')
    server = spawn('provider', [sys.executable, 'scripts/fake_payment_provider.py',
        '--port', str(provider_port), '--ledger', str(run_dir / 'provider.sqlite3')])
    def provider_ready():
        try:
            provider()
            return True
        except OSError:
            return False
    wait_for(provider_ready, 'provider ready')
    provider('/control', {'delay': 30})
    worker = spawn('worker', worker_args)
    event = OutboxEvent.objects.get(topic='refund.requested')
    process_event.delay(event.pk)
    wait_for(lambda: len(provider()) == 1, 'external acceptance persisted')
    stop(worker, hard=True)
    assert not Payment.objects.filter(kind='refund').exists()
    assert RefundObligation.objects.get(pk=operation_data['obligation_id']).status == 'submitting'
    provider('/control', {'delay': 0})
    worker = spawn('worker-restarted', worker_args)
    beat = spawn('beat', [sys.executable, '-m', 'celery', '-A', 'smartcare', 'beat',
        '--loglevel=INFO', '--schedule', str(run_dir / 'beat')])
    wait_for(lambda: RefundObligation.objects.get(pk=operation_data['obligation_id']).status == 'refunded',
             'refund recovered after worker death')
    wait_for(lambda: OutboxEvent.objects.get(pk=event.pk).processed_at is not None, 'refund outbox acknowledged')
    ledger = provider()
    assert len(ledger) == 1 and ledger[0]['calls'] >= 2, ledger
    assert Payment.objects.filter(kind='refund').count() == 1
    report['checks']['worker_death_after_acceptance'] = {
        'passed': True, 'accepted_operations': len(ledger), 'provider_calls': ledger[0]['calls'],
        'refund_ledger_entries': Payment.objects.filter(kind='refund').count()}
    # Real broker outage while the app commits work. Beat must recover it without a publish succeeding.
    stop(redis_proc)
    backlog = [OutboxEvent.objects.create(key=f'recovery-backlog-{i}', topic='demo.ping', payload={}).pk for i in range(25)]
    poison = OutboxEvent.objects.create(key='recovery-poison', topic='unsupported.recovery', payload={})
    time.sleep(2)
    assert OutboxEvent.objects.filter(pk__in=backlog, processed_at__isnull=True).count() == 25
    redis_proc = spawn('redis-restarted', redis_args)
    wait_for(ready, 'broker restarted')
    wait_for(lambda: not OutboxEvent.objects.filter(pk__in=backlog, processed_at__isnull=True).exists(),
        'committed backlog drained after broker restoration', timeout=60)
    wait_for(lambda: OutboxEvent.objects.get(pk=poison.pk).attempts > 0, 'poison event deferred')
    report['checks']['broker_restart_and_backlog'] = {'passed': True, 'recovered_events': len(backlog)}
    report['checks']['poison_event_isolation'] = {'passed': True}
    batch1 = create_settlement(fixture.finance, fixture.facility.pk, 'recovery-batch')
    batch2 = create_settlement(fixture.finance, fixture.facility.pk, 'recovery-batch')
    assert batch1 == batch2 and SettlementLine.objects.filter(original_payable=fixture.payable).count() == 1
    report['checks']['original_allocation_replay'] = {'passed': True}
    report['passed'] = True
finally:
    for proc in reversed(processes):
        stop(proc)
    runner.teardown_databases(old_config)
    (run_dir / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
