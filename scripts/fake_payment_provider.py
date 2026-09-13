"""Loopback-only failure injection provider. Its ledger survives application/worker failures."""
import argparse
import hashlib
import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=8099)
parser.add_argument('--ledger', required=True)
args = parser.parse_args()
with sqlite3.connect(args.ledger) as db:
    db.execute('CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, payload TEXT, status TEXT, reference TEXT, calls INTEGER)')
control = {'status': 'succeeded', 'delay': 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        with sqlite3.connect(args.ledger) as db:
            rows = db.execute('SELECT key,status,reference,calls FROM operations').fetchall()
        self.reply(200, [{'key': x[0], 'status': x[1], 'reference': x[2], 'calls': x[3]} for x in rows])

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/control':
            control.update(data)
            self.reply(200, control)
            return
        key = data['key']
        payload = json.dumps(data, sort_keys=True)
        with sqlite3.connect(args.ledger, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload,status,reference FROM operations WHERE key=?', (key,)).fetchone()
            if row and row[0] != payload:
                self.reply(409, {'error': 'identity conflict'})
                return
            if row:
                status, reference = row[1:]
                db.execute('UPDATE operations SET calls=calls+1 WHERE key=?', (key,))
            else:
                status = control['status']
                reference = 'fake_' + hashlib.sha256(key.encode()).hexdigest()[:32]
                db.execute('INSERT INTO operations VALUES (?,?,?,?,1)', (key, payload, status, reference))
        # Acceptance is committed independently BEFORE injecting timeout or process death.
        time.sleep(control['delay'])
        self.reply(200, {'status': status, 'reference': reference})


ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
