"""Authenticated, streaming MariaDB backup. Restore only into a new restore_* database."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import threading
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
MAGIC = b'SCBAK1'
CHUNK = 1024 * 1024


def identifier(name):
    if not re.fullmatch(r'[a-zA-Z0-9_]+', name):
        raise ValueError('Invalid database identifier.')
    return name


def backup(database, defaults, key_file, output, dump_binary='mariadb-dump'):
    identifier(database)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists():
        raise ValueError('Refusing to overwrite an existing backup.')
    key = bytes.fromhex(Path(key_file).read_text().strip())
    if len(key) != 32:
        raise ValueError('A 256-bit backup key is required.')
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    started = datetime.now(timezone.utc).isoformat()
    begin = time.monotonic()
    partial = output.with_name(output.name + '.' + nonce.hex() + '.partial')
    try:
        with tempfile.TemporaryFile() as errors, partial.open('xb') as target:
            os.chmod(partial, 0o600)
            target.write(MAGIC + nonce)
            process = subprocess.Popen([dump_binary, '--defaults-extra-file=' + str(defaults),
                '--single-transaction', '--quick', '--skip-lock-tables', '--skip-triggers',
                '--hex-blob', database], stdout=subprocess.PIPE, stderr=errors)
            timer = threading.Timer(int(os.getenv('BACKUP_TIMEOUT_SECONDS', '300')), process.kill)
            timer.daemon = True
            timer.start()
            try:
                while chunk := process.stdout.read(CHUNK):
                    target.write(encryptor.update(chunk))
                if process.wait() != 0:
                    raise RuntimeError('Database dump failed; no completed backup produced.')
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait()
            target.write(encryptor.finalize())
            target.write(encryptor.tag)
            target.flush()
            os.fsync(target.fileno())
        os.link(partial, output)  # Atomic no-overwrite publication.
    finally:
        partial.unlink(missing_ok=True)
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    result = {'completed': True, 'snapshot_start_utc': started,
        'snapshot_end_utc': datetime.now(timezone.utc).isoformat(),
        'backup_seconds': round(time.monotonic() - begin, 3), 'bytes': output.stat().st_size,
        'sha256': digest, 'encryption': 'AES-256-GCM',
        'triggers': 'not included; current Django schema has no triggers'}
    output.with_suffix(output.suffix + '.json').write_text(json.dumps(result, indent=2))
    os.chmod(output.with_suffix(output.suffix + '.json'), 0o600)
    return result


def restore(artifact, defaults, key_file, destination, client_binary='mariadb'):
    identifier(destination)
    if not destination.startswith('restore_'):
        raise ValueError('Restore destination must be a NEW restore_* database.')
    key = bytes.fromhex(Path(key_file).read_text().strip())
    if len(key) != 32:
        raise ValueError('A 256-bit backup key is required.')
    begin = time.monotonic()
    with tempfile.TemporaryFile() as clear, Path(artifact).open('rb') as source:
        if source.read(len(MAGIC)) != MAGIC:
            raise ValueError('Unrecognized backup format.')
        nonce = source.read(12)
        source.seek(-16, 2)
        tag = source.read(16)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(MAGIC)
        size = source.tell() - len(MAGIC) - 12 - 16
        source.seek(len(MAGIC) + 12)
        while size:
            chunk = source.read(min(CHUNK, size))
            if not chunk:
                raise ValueError('Truncated backup.')
            clear.write(decryptor.update(chunk))
            size -= len(chunk)
        clear.write(decryptor.finalize())
        clear.seek(0)
        with tempfile.TemporaryFile() as errors:
            create = subprocess.run([client_binary, '--defaults-extra-file=' + str(defaults),
                '-e', f'CREATE DATABASE {destination} CHARACTER SET utf8mb4'],
                stderr=errors, stdout=subprocess.DEVNULL, timeout=int(os.getenv('BACKUP_TIMEOUT_SECONDS', '300')))
            if create.returncode:
                raise RuntimeError('Destination could not be created; an existing database is never overwritten.')
            result = subprocess.run([client_binary, '--defaults-extra-file=' + str(defaults), destination],
                stdin=clear, stdout=subprocess.DEVNULL, stderr=errors, timeout=int(os.getenv('BACKUP_TIMEOUT_SECONDS', '300')))
            if result.returncode:
                raise RuntimeError('Restore failed; keep the new destination quarantined for investigation.')
    return {'restored': True, 'destination': destination, 'quarantined': True,
            'restore_seconds': round(time.monotonic() - begin, 3)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'restore'])
    parser.add_argument('--database', required=True)
    parser.add_argument('--defaults-file', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--artifact', required=True)
    parser.add_argument('--binary')
    args = parser.parse_args()
    try:
        if args.action == 'backup':
            result = backup(args.database, args.defaults_file, args.key_file, args.artifact, args.binary or 'mariadb-dump')
        else:
            result = restore(args.artifact, args.defaults_file, args.key_file, args.database, args.binary or 'mariadb')
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({'completed': False, 'error_type': type(exc).__name__, 'action': args.action}))
        raise SystemExit(1)
