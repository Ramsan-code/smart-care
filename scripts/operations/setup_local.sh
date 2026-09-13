#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
bash scripts/setup_local.sh
.venv/bin/python -m pip install --no-cache-dir -r requirements-staging.txt
(
  cd .runtime/packages
  apt-get download nginx nginx-common libpcre2-8-0 mariadb-client docker-compose-v2
  for package in nginx_*.deb nginx-common_*.deb libpcre2-8-0_*.deb mariadb-client_*.deb docker-compose-v2_*.deb; do
    dpkg-deb -x "$package" ../services
  done
)
.venv/bin/python scripts/operations/init_secrets.py
