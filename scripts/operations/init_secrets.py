"""Create local-only secrets and a localhost TLS certificate; never overwrite."""
import os
from pathlib import Path
import secrets
import subprocess
root = Path(__file__).resolve().parents[2] / '.runtime/staging-secrets'
root.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(root, 0o700)
for name in ['django_key', 'runtime_db_password', 'db_admin_password', 'health_token', 'payment_callback_secret', 'backup_db_password', 'backup_key']:
    path = root / name
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600 if name == 'backup_key' else 0o444)
if not (root / 'tls_key').exists():
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', str(root / 'tls_key'), '-out', str(root / 'tls_cert'), '-days', '7',
        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (root / 'tls_key').chmod(0o444)
    (root / 'tls_cert').chmod(0o444)
print('Local secrets created/preserved. Certificate is for isolated local validation only.')
