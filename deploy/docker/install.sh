#!/usr/bin/env bash
# Docker install: dedicated non-root user, bind-mounted storage directory, host networking,
# token kept in a file (not the container environment), port and health verified at the end.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common.sh
source "$HERE/../common.sh"

need_root
need_cmd docker
need_cmd curl
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is required (docker-compose-plugin)"

ensure_user
SSA_UID="$(id -u "$SSA_USER")"
SSA_GID="$(id -g "$SSA_USER")"

DEFAULT_STORAGE=/srv/service_status_aggregator
read -r -p "Storage directory for config, database and logs [$DEFAULT_STORAGE]: " STORAGE
STORAGE="${STORAGE:-$DEFAULT_STORAGE}"
[[ "$STORAGE" == /* ]] || die "storage directory must be an absolute path"

ensure_dir "$STORAGE"         "$SSA_USER:$SSA_USER" 0750
ensure_dir "$STORAGE/data"    "$SSA_USER:$SSA_USER" 0750
ensure_dir "$STORAGE/logs"    "$SSA_USER:$SSA_USER" 0750
ensure_dir "$STORAGE/secrets" "$SSA_USER:$SSA_USER" 0700

if [[ ! -f "$STORAGE/config.toml" ]]; then
  log "writing $STORAGE/config.toml"
  sed -e "s|/var/lib/service_status_aggregator/aggregator.db|/data/data/aggregator.db|" \
      -e "s|/var/log/service_status_aggregator/aggregator.log|/data/logs/aggregator.log|" \
      "$HERE/../../config.example.toml" > "$STORAGE/config.toml"
  chown root:"$SSA_USER" "$STORAGE/config.toml"
  chmod 0640 "$STORAGE/config.toml"
else
  log "keeping existing $STORAGE/config.toml"
fi

if [[ ! -f "$STORAGE/secrets/registration_token" ]]; then
  log "generating registration token"
  TOKEN="$(gen_token)"
  ( umask 077; printf '%s' "$TOKEN" > "$STORAGE/secrets/registration_token" )
  chown "$SSA_USER:$SSA_USER" "$STORAGE/secrets/registration_token"
  print_token_once "$TOKEN"
  unset TOKEN
else
  log "keeping existing token at $STORAGE/secrets/registration_token"
fi

log "writing $HERE/.env (no secrets in it)"
cat > "$HERE/.env" <<ENV
SSA_STORAGE_DIR=$STORAGE
SSA_UID=$SSA_UID
SSA_GID=$SSA_GID
ENV

PORT="$(awk -F'=' '/^[[:space:]]*port[[:space:]]*=/{gsub(/[^0-9]/,"",$2); print $2; exit}' "$STORAGE/config.toml")"
PORT="${PORT:-$SSA_PORT_DEFAULT}"
if ! docker ps --format '{{.Names}}' | grep -qx service-status-aggregator; then
  require_port_free "$PORT"
fi

log "building image and starting container"
( cd "$HERE" && docker compose build --quiet && docker compose up -d )

IP="$(tailscale_ip)"; IP="${IP:-127.0.0.1}"
log "waiting for http://$IP:$PORT/health"
if ! wait_health "http://$IP:$PORT/health" 90; then
  warn "the container did not report healthy within 90s"
  docker logs --tail 50 service-status-aggregator || true
  exit 1
fi
if command -v ss >/dev/null 2>&1 && ! ss -H -ltn "sport = :$PORT" | grep -q "$IP:$PORT"; then
  warn "port $PORT is listening but not on $IP; check [server].bind in $STORAGE/config.toml"
  ss -ltn "sport = :$PORT"
  exit 1
fi
log "container is healthy and listening on $IP:$PORT"
print_reachability "$IP" "$PORT"
echo "  Logs:      docker logs -f service-status-aggregator   (also $STORAGE/logs/aggregator.log)"
echo "  Config:    $STORAGE/config.toml  (edit, then: docker compose -f $HERE/docker-compose.yml restart)"
echo "  Rotate:    write a new token to $STORAGE/secrets/registration_token, then restart"
