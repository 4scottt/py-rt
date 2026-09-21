#!/usr/bin/env bash
# Run the suite against a real MariaDB, as CI does.
#   scripts/test.sh                       # everything
#   scripts/test.sh tests/test_seed.py -k seed
#
# If a pull stalls at "pulling", Docker Desktop's credential helper is
# hanging: rerun with a bare DOCKER_CONFIG.
#   export DOCKER_CONFIG=$(mktemp -d); echo '{}' > "$DOCKER_CONFIG/config.json"
#   ln -s ~/.docker/cli-plugins "$DOCKER_CONFIG/cli-plugins"
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE="docker compose -f deploy/compose.yaml"
$COMPOSE up -d db

printf 'waiting for the database'
for _ in $(seq 1 60); do
  status="$($COMPOSE ps --format '{{.Health}}' db 2>/dev/null || true)"
  if [ "$status" = "healthy" ]; then echo; break; fi
  printf '.'
  sleep 1
done

# deploy/initdb-test.sql makes pyrt_test when the volume is initialised; this
# covers a volume that predates it. The suite truncates every table before
# each test, so it gets a database of its own beside the dev one.
$COMPOSE exec -T db mariadb -uroot -ppyrtroot -e \
  "CREATE DATABASE IF NOT EXISTS pyrt_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   GRANT ALL ON pyrt_test.* TO 'pyrt'@'%';" >/dev/null 2>&1 || true

export TEST_DSN="${TEST_DSN:-mysql+pymysql://pyrt:pyrt@127.0.0.1:3308/pyrt_test?charset=utf8mb4}"
exec uv run pytest "$@"
