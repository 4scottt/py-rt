#!/usr/bin/env bash
# Bring up MariaDB and serve against it.
#   scripts/dev.sh            # http://localhost:8082
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

export PORT="${PORT:-8082}"
export DATABASE_URL="${DATABASE_URL:-mysql+pymysql://pyrt:pyrt@127.0.0.1:3308/pyrt?charset=utf8mb4}"
export BASE_URL="${BASE_URL:-http://localhost:$PORT}"
export SITE_NAME="${SITE_NAME:-localhost}"
export SESSION_SECRET="${SESSION_SECRET:-devsecret}"
export ROOT_PASSWORD="${ROOT_PASSWORD:-password}"
export WORKERS="${WORKERS:-1}"
export TZ="${TZ:-UTC}"

echo "serving on $BASE_URL (sign in as root / $ROOT_PASSWORD)"
exec uv run pyrt serve
