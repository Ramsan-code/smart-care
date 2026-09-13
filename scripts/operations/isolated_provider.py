"""Private synthetic provider. No live payment or notification integrations."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
if os.getenv('ISOLATED_TEST') != 'true':
    raise SystemExit('Explicit isolation required.')
secret = os.getenv('PAYMENT_CALLBACK_SECRET', '')
if os.getenv('PAYMENT_CALLBACK_SECRET_FILE'):
    secret = Path(os.environ['PAYMENT_CALLBACK_SECRET_FILE']).read_text().strip()
ledger = os.environ['PROVIDER_LEDGER']
with sqlite3.connect(ledger) as db:
    db.execute('CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, kind TEXT, payload TEXT, reference TEXT, calls INTEGER)')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/callback':
            body = json.dumps(data, sort_keys=True, separators=(',', ':'))
            result = {'body': body, 'signature': 'sha256=' + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()}
        else:
            key = data['key']
            payload = json.dumps(data, sort_keys=True)
            with sqlite3.connect(ledger, timeout=5) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT reference,payload,kind FROM operations WHERE key=?', (key,)).fetchone()
                if row and (row[1] != payload or row[2] != self.path):
                    self.send_response(409)
                    self.end_headers()
                    return
                reference = row[0] if row else 'isolated_' + hashlib.sha256(key.encode()).hexdigest()[:32]
                if row:
                    db.execute('UPDATE operations SET calls=calls+1 WHERE key=?', (key,))
                else:
                    db.execute('INSERT INTO operations VALUES (?,?,?,?,1)', (key, self.path, payload, reference))
            result = {'reference': reference, 'status': 'pending' if self.path == '/checkout' else 'succeeded'}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


ThreadingHTTPServer((os.getenv('PROVIDER_BIND', '127.0.0.1'), int(os.getenv('PROVIDER_PORT', '8099'))), Handler).serve_forever()
