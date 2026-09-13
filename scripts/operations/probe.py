import os
import socket
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.argv[1] == 'live':
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        sock.connect(os.getenv('GUNICORN_SOCKET', '/run/smartcare/gunicorn.sock'))
        host = os.getenv('ALLOWED_HOSTS', 'localhost').split(',')[0]
        sock.sendall(('GET /health/live/ HTTP/1.0\r\nHost: ' + host + '\r\n\r\n').encode())
        assert b'200' in sock.recv(1024).split(b'\r\n')[0]
else:
    import django
    django.setup()
    from operations.telemetry import client
    key = 'sc:worker' if sys.argv[1] == 'worker' else 'sc:beat'
    stamp = client().get(key)
    assert stamp and time.time() - float(stamp) < 45, 'Processing heartbeat is stale.'
