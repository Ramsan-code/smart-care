#!/usr/bin/env python3
"""Isolated HTTP load test. Never run alongside manage.py test (both use test_smartcare)."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict, Counter
from datetime import timedelta
from http.cookiejar import CookieJar, Cookie
import io
import json
import logging
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'smartcare.settings')
# Dedicated Redis databases: never dispatch load-test events to the live worker.
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6380/14'
os.environ['REDIS_CACHE_URL'] = 'redis://127.0.0.1:6380/15'
import django
django.setup()
from django.conf import settings
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.management import call_command
from django.db import connections
from django.db.models import F, Count
from django.test import Client, override_settings
from django.test.runner import DiscoverRunner
from django.test.testcases import LiveServerThread
from django.utils import timezone
from accounts.models import User
from appointments.models import Appointment, Reservation
from scheduling.models import Slot, Session
from core.models import OutboxEvent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--users', type=int, default=20)
    parser.add_argument('--output', default=str(ROOT / '.runtime/load-results.json'))
    args = parser.parse_args()
    if args.seconds < 1 or not 1 <= args.users <= 20:
        parser.error('Use a positive duration and 1–20 users.')
    if settings.DATABASES['default']['NAME'] != 'smartcare' or settings.DATABASES['default']['TEST']['NAME'] != 'test_smartcare':
        raise SystemExit('Refusing unexpected database configuration.')
    if not settings.DEBUG or not settings.DEMO_MODE:
        raise SystemExit('Load testing requires local debug/demo mode.')
    runtime = ROOT / '.runtime/load'
    runtime.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock = (runtime / 'run.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    logging.getLogger('django.request').setLevel(logging.CRITICAL)
    runner = DiscoverRunner(verbosity=0, interactive=False)
    database_config = runner.setup_databases()
    server = None
    processes = []
    observations = []
    observer_lock = threading.Lock()
    failures = []
    initial_slots = 0
    try:
        with override_settings(BASE_DIR=runtime, ALLOWED_HOSTS=['127.0.0.1', 'localhost', 'testserver']):
            today = timezone.localdate()
            call_command('seed_demo', base_date=str(today), stdout=io.StringIO())
            call_command('generate_slots', start_date=str(today), days=30, stdout=io.StringIO())
            initial_slots = Slot.objects.count()
            days = list(Session.objects.filter(starts_at__gt=timezone.now()).order_by('date').values_list('date', flat=True).distinct())
            if not days: raise RuntimeError('No future load-test inventory.')
            sessions = []
            for i in range(1, args.users + 1):
                user = User.objects.get(email=f'patient{i:02}@example.test')
                client = Client(); client.force_login(user)
                sessions.append(client.cookies['sessionid'].value)
            server = LiveServerThread('127.0.0.1', StaticFilesHandler, port=0)
            server.start(); server.is_ready.wait(30)
            if server.error: raise server.error
            origin = f'http://127.0.0.1:{server.port}'
            environment = {**os.environ, 'DB_NAME': 'test_smartcare'}
            commands = [
                [sys.executable, '-m', 'celery', '-A', 'smartcare', 'worker', '--pool=solo', '--loglevel=warning', '--hostname=loadtest@%h'],
                [sys.executable, '-m', 'celery', '-A', 'smartcare', 'beat', '--loglevel=warning', '--schedule', str(runtime / 'celerybeat')],
            ]
            for i, command in enumerate(commands):
                logfile = (runtime / f'celery-{i}.log').open('w')
                processes.append(subprocess.Popen(command, cwd=ROOT, env=environment, stdout=logfile, stderr=logfile))
                logfile.close()
            barrier = threading.Barrier(args.users + 1)
            start = [None]
            def virtual_user(index):
                rng = random.Random(index + 9122026)
                jar = CookieJar()
                jar.set_cookie(Cookie(0, 'sessionid', sessions[index], None, False, '127.0.0.1', False, False,
                                      '/', True, False, None, True, None, None, {}))
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
                def request(label, path, method='GET', body=None):
                    headers = {'Content-Type': 'application/json'}
                    if method != 'GET':
                        headers.update({'X-CSRFToken': next((c.value for c in jar if c.name == 'csrftoken'), ''),
                                        'Idempotency-Key': uuid.uuid4().hex})
                    req = urllib.request.Request(origin + path, method=method, headers=headers,
                                                 data=json.dumps(body).encode() if body is not None else None)
                    t = time.perf_counter(); status = 0; payload = {}
                    try:
                        with opener.open(req, timeout=20) as response:
                            status = response.status; raw = response.read()
                            if 'application/json' in response.headers.get('Content-Type', ''): payload = json.loads(raw)
                    except urllib.error.HTTPError as exc:
                        status = exc.code; raw = exc.read()
                        try: payload = json.loads(raw)
                        except ValueError: pass
                    except Exception as exc:
                        with observer_lock: failures.append(type(exc).__name__)
                    elapsed = (time.perf_counter() - t) * 1000
                    with observer_lock: observations.append((label, status, elapsed))
                    return status, payload
                # Initialize a real authenticated session and CSRF cookie through HTTP before timing.
                status, _ = request('session_setup', '/book/')
                if status != 200: failures.append(f'user_{index}_setup_{status}')
                barrier.wait(timeout=60)
                iteration = 0
                while time.perf_counter() - start[0] < args.seconds:
                    cycle = time.perf_counter()
                    day = days[(iteration + index) % len(days)]
                    status, data = request('availability', f'/api/v1/availability/?facility_id=1&date={day}')
                    free = [s for s in data.get('results', []) if s['status'] == 'available']
                    if status == 200 and free and iteration % 6 == 0:
                        slot = rng.choice(free)
                        status, held = request('hold', '/api/v1/holds/', 'POST',
                                               {'slot_id': slot['id'], 'expected_version': slot['version']})
                        if status == 201:
                            if iteration % 30 == 0:
                                request('confirmation', '/api/v1/appointments/', 'POST',
                                    {'hold_id': held['id'], 'expected_version': held['version'], 'consent_version': 'demo-v1',
                                     'reason_category': 'routine', 'payment_method': 'counter_due'})
                            else:
                                request('release', '/api/v1/holds/' + held['id'] + '/', 'DELETE', {'expected_version': held['version']})
                    if iteration % 10 == 0: request('history', '/api/v1/appointments/')
                    iteration += 1
                    # Closed-loop virtual users: one cycle every two seconds, including response time.
                    remaining = min(2 - (time.perf_counter() - cycle), args.seconds - (time.perf_counter() - start[0]))
                    if remaining > 0: time.sleep(remaining)
                connections.close_all()
            with ThreadPoolExecutor(max_workers=args.users) as pool:
                futures = [pool.submit(virtual_user, i) for i in range(args.users)]
                # Give every authenticated user the same measured start/deadline.
                start[0] = time.perf_counter()
                barrier.wait(timeout=60)
                start[0] = time.perf_counter()
                print(f'LOAD START: {args.users} concurrent users, {args.seconds}s, {initial_slots} slots over 30 days, isolated test_smartcare.', flush=True)
                while not all(f.done() for f in futures):
                    time.sleep(min(30, args.seconds))
                    with observer_lock: count = len(observations)
                    print(f'PROGRESS {min(args.seconds, int(time.perf_counter()-start[0]))}s: {count} HTTP requests', flush=True)
                for future in futures: future.result()
            elapsed_seconds = time.perf_counter() - start[0]
            grouped = defaultdict(list); codes = defaultdict(Counter)
            for label, status, ms in observations:
                grouped[label].append(ms); codes[label][str(status)] += 1
            metrics = {}
            for label, values in grouped.items():
                values.sort()
                metrics[label] = {'requests': len(values), 'status_counts': dict(codes[label]),
                                  'p50_ms': round(values[math.ceil(.5 * len(values)) - 1], 2),
                                  'p95_ms': round(values[math.ceil(.95 * len(values)) - 1], 2),
                                  'max_ms': round(max(values), 2)}
            measured = [o for o in observations if o[0] != 'session_setup']
            unexpected = [o for o in measured if not 200 <= o[1] < 300 and not (o[0] == 'hold' and o[1] == 409)]
            invariants = {
                'duplicate_reservation_appointments': Appointment.objects.values('reservation_id').annotate(n=Count('id')).filter(n__gt=1).count(),
                'booked_reservations_missing_slot': Reservation.objects.filter(status='booked', active_slot__isnull=True).count(),
                'active_pointer_mismatches': Slot.objects.filter(active_reservation__isnull=False).exclude(pk=F('active_reservation__slot_id')).count(),
                'confirmed_without_booked_reservation': Appointment.objects.filter(state='confirmed').exclude(reservation__status='booked').count(),
            }
            result = {'created_at': timezone.now().isoformat(), 'duration_target_seconds': args.seconds,
                      'observed_seconds_including_final_progress_wait': round(elapsed_seconds, 2), 'virtual_users': args.users,
                      'patients': args.users, 'days_of_inventory': 30, 'initial_slots': initial_slots,
                      'request_count': len(measured), 'unexpected_errors': len(unexpected),
                      'unexpected_error_rate_percent': round(100 * len(unexpected) / max(1, len(measured)), 4),
                      'expected_capacity_conflicts': sum(1 for label, status, _ in measured if label == 'hold' and status == 409),
                      'transport_failures': dict(Counter(failures)), 'metrics': metrics, 'invariants': invariants,
                      'confirmed_appointments': Appointment.objects.count(),
                      'pending_outbox_at_finish': OutboxEvent.objects.filter(processed_at__isnull=True).count(),
                      'workload': f'{args.users} authenticated patient sessions; 2-second closed-loop cycles; availability every cycle, history every 10, hold every 6, confirm every 30, otherwise release.',
                      'server': 'Django threaded development WSGI, loopback HTTP, local MariaDB 10.11 and Redis with isolated Celery worker/beat; not a production server benchmark.',
                      'external_payment_or_message_latency': 'Not applicable: Phase 3 counter-due booking, acknowledgment-only outbox.',
                      'passed': not unexpected and not failures and not any(invariants.values())}
            Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2), flush=True)
            return 0 if result['passed'] else 1
    finally:
        for process in reversed(processes): process.terminate()
        for process in reversed(processes):
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        if server: server.terminate(); server.join(timeout=10)
        connections.close_all()
        runner.teardown_databases(database_config)


if __name__ == '__main__':
    raise SystemExit(main())
