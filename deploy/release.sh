#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:-staging}"
case "$mode" in
  staging) compose=(docker compose -p smartcare-staging -f deploy/compose.staging.yml) ;;
  isolated) compose=(docker compose -p smartcare-isolated -f deploy/compose.staging.yml -f deploy/compose.isolated.yml) ;;
  *) echo "Use staging or isolated."; exit 2 ;;
esac
: "${RELEASE_TAG:?Set an immutable release tag before building.}"
"${compose[@]}" config --quiet
"${compose[@]}" build
# Maintenance window: stop old writers before enforcing financial schema constraints.
"${compose[@]}" stop proxy web worker beat
"${compose[@]}" up -d db redis
"${compose[@]}" --profile release run --rm migrate
"${compose[@]}" --profile release run --rm provision
if [[ "$mode" == isolated ]]; then "${compose[@]}" up -d test-provider; fi
"${compose[@]}" up -d web worker beat proxy
# A disabled staging provider must keep readiness blocked. Never suppress that result.
curl --fail --retry 12 --retry-all-errors --retry-delay 5 --cacert .runtime/staging-secrets/tls_cert https://localhost:8443/health/ready/
