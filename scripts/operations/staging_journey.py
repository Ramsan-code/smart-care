"""Synthetic HTTPS journey and authenticated backup/restore; owns test_smartcare exclusively."""
import getpass
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import redis
from urllib.request import Request, build_opener, HTTPCookieProcessor, HTTPSHandler, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
for key, value in dotenv_values(ROOT / '.env').items():
    if value is not None:
        os.environ.setdefault(key, value)
run = Path(tempfile.mkdtemp(prefix='staging-', dir=ROOT/'.runtime'))
def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]
web_port, broker_port, provider_port = port(), port(), port()
secret_dir = ROOT/'.runtime/staging-secrets'
os.environ.update(DJANGO_SETTINGS_MODULE='smartcare.settings_isolated', APP_ENV='isolated',
    ISOLATED_TEST='true', DB_NAME='test_smartcare', ALLOWED_HOSTS='localhost',
    CSRF_TRUSTED_ORIGINS=f'https://localhost:{web_port}', ISOLATED_PROVIDER_URL=f'http://127.0.0.1:{provider_port}',
    DJANGO_SECRET_KEY_FILE=str(secret_dir/'django_key'), HEALTH_TOKEN_FILE=str(secret_dir/'health_token'),
    PAYMENT_CALLBACK_SECRET_FILE=str(secret_dir/'payment_callback_secret'),
    REDIS_URL=f'redis://127.0.0.1:{broker_port}/0', REDIS_CACHE_URL=f'redis://127.0.0.1:{broker_port}/1',
    OPERATIONS_REDIS_URL=f'redis://127.0.0.1:{broker_port}/2', OUTBOX_RECOVERY_SECONDS='1')
import django
django.setup()
from django.conf import settings
from django.db import connection
from django.test.runner import DiscoverRunner
from django.utils import timezone
from accounts.models import User, Membership, DemoEmail
from appointments.models import Appointment
from communications.models import Delivery
from core.models import OutboxEvent, AuditEvent
from finance.models import Payment, RefundObligation, SettlementLine, SettlementBatch
from scripts.operations.provision_db import grant_runtime
from scripts.operations.backup import backup, restore

report = {'checks': {}, 'run_directory': str(run)}
processes = []
runtime_user = 'sc_runtime_' + secrets.token_hex(4)
backup_user = 'sc_backup_' + secrets.token_hex(4)
runtime_password, backup_password = secrets.token_hex(24), secrets.token_hex(24)
restore_name = 'restore_smartcare_' + secrets.token_hex(4)
env = dict(os.environ)
env['LD_LIBRARY_PATH'] = str(ROOT/'.runtime/services/usr/lib/x86_64-linux-gnu')
os.environ['LD_LIBRARY_PATH'] = env['LD_LIBRARY_PATH']
for name, value in [('runtime_password', runtime_password), ('backup_password', backup_password)]:
    (run/name).write_text(value)
    (run/name).chmod(0o600)
env['DB_USER'] = runtime_user
env['DB_PASSWORD_FILE'] = str(run/'runtime_password')
env.update(PROVIDER_LEDGER=str(run/'provider.sqlite3'), PROVIDER_PORT=str(provider_port),
           GUNICORN_SOCKET=str(run/'app.sock'), WEB_WORKERS='2')
runner = DiscoverRunner(verbosity=0, interactive=False)
old_config = runner.setup_databases()
admin = None


def spawn(name, args):
    logfile = (run/(name+'.log')).open('ab')
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=logfile, stderr=logfile, start_new_session=True)
    processes.append(proc)
    return proc


def stop(proc):
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def wait_for(fn, label, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if fn():
                return
        except (OSError, ConnectionError, redis.RedisError, AssertionError):
            pass
        time.sleep(.2)
    raise AssertionError('Timed out: ' + label)


def provider(path, data):
    request = Request(settings.ISOLATED_PROVIDER_URL + path, data=json.dumps(data).encode(),
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=5) as response:
        return json.load(response)


context = ssl.create_default_context(cafile=str(secret_dir/'tls_cert'))
origin = f'https://localhost:{web_port}'


class Browser:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar), HTTPSHandler(context=context))

    def request(self, path, data=None, *, form=False, key=None, status=200, headers=None):
        values = dict(headers or {})
        if data is not None:
            values.update({'Referer': origin+'/', 'X-CSRFToken': next((c.value for c in self.jar if c.name=='csrftoken'), '')})
            values['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
            body = data if isinstance(data, bytes) else urlencode(data).encode() if form else json.dumps(data).encode()
        else:
            body = None
        if key:
            values['Idempotency-Key'] = key
        request = Request(origin+path, data=body, headers=values)
        try:
            response = self.opener.open(request, timeout=10)
        except HTTPError as exc:
            response = exc
        raw = response.read()
        assert response.status == status, (path, response.status, raw[:160])
        self.last_headers = response.headers
        return json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else raw

    def login(self, user, password):
        self.request('/accounts/login/')
        self.request('/accounts/login/', {'username': user.email, 'password': password}, form=True)


def fingerprint(cursor):
    tables = ['appointments_appointment', 'appointments_reservation', 'finance_payment',
              'finance_refundobligation', 'finance_doctorpayable', 'finance_settlementbatch',
              'finance_settlementline', 'core_auditevent', 'core_outboxevent']
    result = {}
    for table in tables:
        cursor.execute(f'SELECT * FROM {table} ORDER BY 1')
        rows = cursor.fetchall()
        result[table] = {'rows': len(rows), 'sha256': hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()}
    return result


try:
    import MySQLdb
    if os.getenv('DB_USER') == 'root':
        admin = MySQLdb.connect(host=os.environ['DB_HOST'], port=int(os.environ['DB_PORT']),
                                user='root', passwd=os.environ['DB_PASSWORD'])
        admin_options = f"host={os.environ['DB_HOST']}\nport={os.environ['DB_PORT']}\nuser=root\npassword={os.environ['DB_PASSWORD']}\n"
    else:
        admin = MySQLdb.connect(unix_socket=str(ROOT/'.runtime/mysql.sock'), user=getpass.getuser())
        admin_options = f"socket={ROOT/'.runtime/mysql.sock'}\nuser={getpass.getuser()}\n"
    with admin.cursor() as cursor:
        grant_runtime(cursor, 'test_smartcare', runtime_user, runtime_password)
        grant_runtime(cursor, 'test_smartcare', backup_user, backup_password, backup=True)
    from appointments import tests as fixtures
    from datetime import timedelta
    fixtures.NOW = timezone.now()
    today = timezone.localdate()
    fixtures.DAY = today + timedelta(days=(7-today.weekday()) % 7 or 7)
    class Fixture(fixtures.BookingFixture, unittest.TestCase):
        pass
    fixture = Fixture()
    fixture.setUp()
    fixture.doCleanups()
    admin_password = secrets.token_urlsafe(24)
    fixture.admin.set_password(admin_password)
    fixture.admin.save()
    finance = User.objects.create_user('stage-finance@example.test', role='finance', password=admin_password)
    Membership.objects.create(user=finance, facility=fixture.facility, finance_approver=True)
    # Prove runtime schema and ledger mutation restrictions without altering any data.
    limited = MySQLdb.connect(host=os.environ.get('DB_HOST', '127.0.0.1'), port=int(os.environ.get('DB_PORT', '3307')),
        user=runtime_user, passwd=runtime_password, db='test_smartcare')
    for sql in ['CREATE TABLE should_be_denied(id INT)', 'UPDATE finance_payment SET amount=0 WHERE 1=0']:
        try:
            limited.cursor().execute(sql)
        except MySQLdb.OperationalError as exc:
            assert exc.args[0] in [1142, 1044], exc.args[0]
        else:
            raise AssertionError('Runtime database privilege was too broad.')
    limited.close()
    report['checks']['restricted_runtime_grants'] = True
    redis_binary = ROOT/'.runtime/services/usr/bin/redis-server'
    broker = spawn('redis', [str(redis_binary), '--bind','127.0.0.1','--port',str(broker_port),
                            '--dir',str(run),'--appendonly','yes','--save',''])
    import redis
    store = redis.Redis(host='127.0.0.1', port=broker_port)
    wait_for(lambda: store.ping(), 'broker')
    provider_process = spawn('provider', [sys.executable, 'scripts/operations/isolated_provider.py'])
    wait_for(lambda: provider('/callback', {'probe': True}), 'isolated provider')
    subprocess.run([sys.executable, 'manage.py', 'check', '--deploy'], cwd=ROOT, env=env, check=True,
                   stdout=(run/'deployment-checks.log').open('w'), stderr=subprocess.STDOUT)
    subprocess.run([sys.executable, 'manage.py', 'collectstatic', '--noinput'], cwd=ROOT, env=env, check=True,
                   stdout=subprocess.DEVNULL)
    web = spawn('gunicorn', [sys.executable,'-m','gunicorn','-c','deploy/gunicorn.conf.py','smartcare.wsgi:application'])
    wait_for(lambda: (run/'app.sock').exists(), 'Gunicorn')
    nginx_config = (ROOT/'deploy/nginx.conf').read_text()
    replacements = {
        '/tmp/nginx.pid': str(run/'nginx.pid'),
        '/etc/nginx/mime.types': str(ROOT/'.runtime/services/etc/nginx/mime.types'),
        '/tmp/client_body': str(run/'client_body'), '/tmp/proxy': str(run/'proxy'),
        '/tmp/fastcgi': str(run/'fastcgi'), '/tmp/uwsgi': str(run/'uwsgi'), '/tmp/scgi': str(run/'scgi'),
        '/run/smartcare/gunicorn.sock': str(run/'app.sock'),
        'listen 8443 ssl;': f'listen 127.0.0.1:{web_port} ssl;',
        '/run/secrets/tls_cert': str(secret_dir/'tls_cert'),
        '/run/secrets/tls_key': str(secret_dir/'tls_key'), '/srv/static/': str(ROOT/'.runtime/static')+'/',
    }
    for old, new in replacements.items():
        nginx_config = nginx_config.replace(old,new)
    (run/'nginx.conf').write_text(nginx_config)
    nginx = spawn('nginx', [str(ROOT/'.runtime/services/usr/sbin/nginx'), '-c',str(run/'nginx.conf'),
                           '-p',str(run)+'/', '-g','daemon off;'])
    browser = Browser()
    wait_for(lambda: browser.request('/health/live/'), 'TLS liveness')
    worker_args = [sys.executable,'-m','celery','-A','smartcare','worker','--concurrency=1','--loglevel=INFO']
    worker = spawn('worker',worker_args)
    beat = spawn('beat',[sys.executable,'-m','celery','-A','smartcare','beat','--loglevel=INFO','--schedule',str(run/'beat')])
    wait_for(lambda: all(__import__('operations.views', fromlist=['dependency_state']).dependency_state().values()), 'readiness')
    browser.request('/health/ready/', headers={'X-Forwarded-Proto': 'http', 'X-Forwarded-Host': 'evil.invalid'})
    browser.request('/health/ready/', headers={'Host':'evil.invalid'}, status=400)
    assert b'bootstrap' in browser.request('/static/vendor/bootstrap.min.css')[:500].lower()
    report['checks']['https_proxy_static_health_and_spoofing'] = True
    browser.request('/accounts/register/')
    patient_password = secrets.token_urlsafe(24)
    browser.request('/accounts/register/', {'first_name':'Synthetic','last_name':'Journey',
        'email':'stage-patient@example.test','phone':'+94770000000','facility':fixture.facility.pk,
        'password1':patient_password,'password2':patient_password,'consent':'on'}, form=True)
    patient = User.objects.get(email='stage-patient@example.test')
    mail = DemoEmail.objects.filter(recipient=patient.email).latest('pk')
    verify_path = re.search(r'/accounts/verify/[^\s]+', mail.body).group(0)
    assert 'https://localhost:' in mail.body
    browser.request(verify_path)
    browser.request(verify_path, {}, form=True)
    patient.refresh_from_db()
    assert patient.email_verified
    assert all(cookie.secure for cookie in browser.jar if cookie.name in ['sessionid','csrftoken'])
    hold = browser.request('/api/v1/holds/', {'slot_id':str(fixture.slot.pk),'expected_version':fixture.slot.version},
                           key='journey-hold',status=201)
    payload = {'hold_id':hold['id'],'expected_version':hold['version'],'payment_method':'counter_due',
               'consent_version':'demo-v1','reason_category':'new_visit'}
    booking = browser.request('/api/v1/appointments/',payload,key='journey-confirm',status=201)
    replay = browser.request('/api/v1/appointments/',payload,key='journey-confirm',status=201)
    assert booking['id'] == replay['id']
    appointment_id = booking['id']
    checkout = browser.request('/api/v1/payments/checkouts/',
        {'appointment_id':appointment_id,'expected_version':booking['version']}, key='journey-checkout',status=201)
    from finance.models import Checkout
    checkout_row = Checkout.objects.get(pk=checkout['id'])
    callback = provider('/callback', {'checkout_id':str(checkout_row.pk),'event_id':'journey-callback',
        'provider_reference':checkout_row.provider_reference,'status':'succeeded',
        'amount':str(checkout_row.amount),'currency':checkout_row.currency})
    for _ in range(2):
        browser.request('/api/v1/payments/callback/',callback['body'].encode(),
            headers={'X-Payment-Signature':callback['signature']})
    assert Payment.objects.filter(appointment_id=appointment_id,kind='charge').count() == 1
    wait_for(lambda: Delivery.objects.filter(appointment_id=appointment_id,event='confirmation',status='sent').exists(), 'confirmation delivery')
    appointment = Appointment.objects.get(pk=appointment_id)
    from unittest.mock import patch
    from communications.tasks import queue_due_reminders
    with patch('django.utils.timezone.now',return_value=appointment.starts_at-timedelta(minutes=10)):
        queue_due_reminders()
    OutboxEvent.objects.filter(topic='appointment.reminder').update(available_at=timezone.now())
    wait_for(lambda: Delivery.objects.filter(appointment=appointment,event='reminder',status='sent').exists(), 'reminder delivery')
    stop(worker)
    worker = spawn('worker-restarted',worker_args)
    outbox = OutboxEvent.objects.create(key='restart-canary',topic='demo.ping',payload={})
    wait_for(lambda: OutboxEvent.objects.get(pk=outbox.pk).processed_at is not None, 'worker restart')
    staff = Browser()
    staff.login(fixture.admin,admin_password)
    for state in ['arrived','waiting','in_consultation','completed']:
        appointment.refresh_from_db()
        staff.request(f'/api/v1/appointments/{appointment_id}/operations/{state}/',
                      {'expected_version':appointment.version})
    accountant = Browser()
    accountant.login(finance,admin_password)
    payables = accountant.request('/api/v1/finance/payables/',{'facility_id':fixture.facility.pk},status=201)
    payable_id = payables['results'][0]['id']
    batch = accountant.request('/api/v1/finance/settlements/',
        {'facility_id':fixture.facility.pk,'reference':'JOURNEY'},key='settlement-create',status=201)
    def settle(batch_id, key):
        accountant.request(f'/api/v1/finance/settlements/{batch_id}/review/',{},key=key+'review')
        staff.request(f'/api/v1/finance/settlements/{batch_id}/approve/',{},key=key+'approve')
        accountant.request(f'/api/v1/finance/settlements/{batch_id}/paid/',{},key=key+'paid')
    settle(batch['id'],'original')
    export = accountant.request(f"/api/v1/finance/settlements/{batch['id']}/export/")
    assert b'2000.00' in export
    appointment.refresh_from_db()
    refunded = staff.request(f'/api/v1/appointments/{appointment_id}/refunds/',
        {'expected_version':appointment.version,'amount':'500.00'},key='journey-refund',status=201)
    wait_for(lambda: RefundObligation.objects.get(pk=refunded['obligation_id']).status == 'refunded', 'refund')
    payment = Payment.objects.get(obligation_id=refunded['obligation_id'])
    adjustment = accountant.request('/api/v1/finance/settlements/refund-adjustments/',
        {'paid_batch_id':batch['id'],'payable_id':payable_id,'amount':'400.00','refund_id':str(payment.pk)},
        key='journey-adjustment',status=201)
    settle(adjustment['id'],'adjustment')
    assert SettlementLine.objects.get(refund=payment).amount == -400
    assert SettlementBatch.objects.get(pk=batch['id']).status == 'paid'
    assert AuditEvent.objects.filter(action='settlement.approve').count() == 2
    report['checks']['synthetic_end_to_end'] = True
    report['journey'] = {'original_settlement':'2000.00','refund':'500.00','doctor_adjustment':'-400.00',
                         'charges':1,'successful_refunds':1,'paid_batches':2,'notification_events_sent':2}
    stop(beat); stop(worker)
    appointment.refresh_from_db()
    pending = staff.request(f'/api/v1/appointments/{appointment_id}/refunds/',
        {'expected_version':appointment.version,'amount':'100.00'},key='restore-pending',status=201)
    stop(nginx); stop(web)
    source_config = run/'backup.cnf'
    source_config.write_text(f"[client]\nhost={os.environ.get('DB_HOST','127.0.0.1')}\nport={os.environ.get('DB_PORT','3307')}\nuser={backup_user}\npassword={backup_password}\n")
    source_config.chmod(0o600)
    admin_config = run/'admin.cnf'
    admin_config.write_text('[client]\n'+admin_options)
    admin_config.chmod(0o600)
    with connection.cursor() as cursor:
        expected = fingerprint(cursor)
    artifact = run/'snapshot.enc'
    report['backup'] = backup('test_smartcare',source_config,secret_dir/'backup_key',artifact,
                             str(ROOT/'.runtime/services/usr/bin/mariadb-dump'))
    # Provider accepts after the snapshot. Restore must not replay its now-stale pending work.
    operation = RefundObligation.objects.get(pk=pending['obligation_id'])
    provider('/refund',{'key':f'refund:{operation.pk}','payment_reference':operation.source_payment.provider_reference,'amount':str(operation.amount)})
    with sqlite3.connect(run/'provider.sqlite3') as ledger:
        calls_before = ledger.execute('SELECT sum(calls) FROM operations').fetchone()[0]
    began = time.monotonic()
    report['restore'] = restore(artifact,admin_config,secret_dir/'backup_key',restore_name,
                               str(ROOT/'.runtime/services/usr/bin/mariadb'))
    with admin.cursor() as cursor:
        cursor.execute('USE '+restore_name)
        actual = fingerprint(cursor)
    assert actual == expected
    report['restore']['verified_seconds'] = round(time.monotonic()-began,3)
    report['restore']['tables'] = actual
    restored_env = dict(env,DB_NAME=restore_name,RESTORE_QUARANTINE='true')
    # Read-only validation uses an administrative socket in this disposable environment.
    if os.getenv('DB_USER') == 'root':
        restored_env['DB_USER']='root'; restored_env['DB_PASSWORD_FILE']=''
        restored_env['DB_PASSWORD']=os.environ['DB_PASSWORD']
    else:
        restored_env.update(DB_USER=getpass.getuser(),DB_HOST=str(ROOT/'.runtime/mysql.sock'),DB_PASSWORD_FILE='',DB_PASSWORD='')
    verify_code = """import django; django.setup()
from core.tasks import dispatch_outbox,process_event
from core.models import OutboxEvent
from finance.refunds import process_refund
from finance.models import RefundObligation
pending=list(OutboxEvent.objects.filter(processed_at__isnull=True).values_list('pk',flat=True))
dispatch_outbox()
for key in pending: process_event(key)
for key in RefundObligation.objects.filter(status='pending').values_list('pk',flat=True):
 assert process_refund(key)=='quarantined'
assert OutboxEvent.objects.filter(pk__in=pending,processed_at__isnull=False).count()==0
print('Restore quarantine verified without outbound submissions.')
"""
    subprocess.run([sys.executable,'-c',verify_code],cwd=ROOT,env=restored_env,check=True,
                   stdout=(run/'quarantine.log').open('w'),stderr=subprocess.STDOUT)
    with sqlite3.connect(run/'provider.sqlite3') as ledger:
        assert calls_before == ledger.execute('SELECT sum(calls) FROM operations').fetchone()[0]
    report['checks']['restored_rows_hashes_and_quarantine'] = True
    report['passed'] = True
finally:
    for process in reversed(processes): stop(process)
    if admin:
        with admin.cursor() as cursor:
            cursor.execute('DROP DATABASE IF EXISTS '+restore_name)
            for user in [runtime_user,backup_user]:
                cursor.execute(f"DROP USER IF EXISTS '{user}'@'%'")
        admin.close()
    runner.teardown_databases(old_config)
    (run/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
