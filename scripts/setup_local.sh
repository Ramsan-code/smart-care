#!/usr/bin/env bash
# Ubuntu 24.04 x86_64 project-local setup. No sudo or system service changes.
set -euo pipefail
cd "$(dirname "$0")/.."
project_dir="$PWD"
command -v python3 >/dev/null
command -v gcc >/dev/null
command -v dpkg-deb >/dev/null
python3 -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 is required"'
mkdir -p .runtime/packages .runtime/services .runtime/db
if [ ! -x .runtime/services/usr/sbin/mariadbd ]; then
  (
    cd .runtime/packages
    apt-get download mariadb-server-core mariadb-client-core mariadb-common libmariadb3 libmariadb-dev libaio1t64 liburing2 redis-server redis-tools liblzf1 liblua5.1-0 libjemalloc2
    for package in *.deb; do dpkg-deb -x "$package" ../services; done
  )
fi
export LD_LIBRARY_PATH="$project_dir/.runtime/services/usr/lib/x86_64-linux-gnu"
export MYSQLCLIENT_CFLAGS="-I$project_dir/.runtime/services/usr/include/mariadb"
export MYSQLCLIENT_LDFLAGS="-L$project_dir/.runtime/services/usr/lib/x86_64-linux-gnu -Wl,-rpath,$project_dir/.runtime/services/usr/lib/x86_64-linux-gnu -lmariadb"
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/pip install --no-cache-dir -r requirements.txt
.venv/bin/python - <<'PY'
from pathlib import Path
import secrets
p=Path('.env')
if not p.exists():
    text=Path('.env.example').read_text()
    text=text.replace('replace-with-a-random-generated-secret',secrets.token_urlsafe(48))
    text=text.replace('replace-with-a-random-database-password',secrets.token_hex(24))
    p.write_text(text)
    p.chmod(0o600)
PY
if [ ! -d .runtime/db/mysql ]; then
  .runtime/services/usr/bin/mariadb-install-db --no-defaults --basedir="$project_dir/.runtime/services/usr" --datadir="$project_dir/.runtime/db" --auth-root-authentication-method=socket --skip-test-db
fi
printf '%s\n' 'Setup complete. Run: bash start.sh'
