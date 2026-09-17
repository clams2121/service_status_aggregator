#!/usr/bin/env bash
# Install the Service Status Aggregator as a hardened systemd service.
# Idempotent: re-run after pulling a new version to upgrade in place.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck source=../common.sh
source "$HERE/../common.sh"

APP_DIR=/opt/service_status_aggregator
ETC_DIR=/etc/service_status_aggregator
DATA_DIR=/var/lib/service_status_aggregator
LOG_DIR=/var/log/service_status_aggregator
CRED_DIR=/etc/credstore.encrypted
CRED_FILE="$CRED_DIR/ssa-registration_token"
UNIT=service-status-aggregator.service

need_root
need_cmd systemctl
need_cmd systemd-creds "systemd 250 or newer is required for encrypted credentials"
need_cmd curl

# uv is needed to build the virtualenv. Never pipe curl to sh on the user's behalf.
if ! command -v uv >/dev/null 2>&1; then
  for candidate in /root/.local/bin/uv /usr/local/bin/uv "${SUDO_USER:+/home/$SUDO_USER/.local/bin/uv}"; do
    [[ -n "$candidate" && -x "$candidate" ]] && { UV="$candidate"; break; }
  done
  [[ -n "${UV:-}" ]] || die "uv not found. Install it first (https://docs.astral.sh/uv/getting-started/installation/), e.g.:
    curl -LsSf https://astral.sh/uv/install.sh | sh
  then re-run this script."
else
  UV="$(command -v uv)"
fi

ensure_user
ensure_dir "$ETC_DIR"  "root:$SSA_USER" 0750
ensure_dir "$DATA_DIR" "$SSA_USER:$SSA_USER" 0750
ensure_dir "$LOG_DIR"  "$SSA_USER:$SSA_USER" 0750
ensure_dir "$CRED_DIR" "root:root" 0700

log "installing code to $APP_DIR"
mkdir -p "$APP_DIR"
for item in pyproject.toml uv.lock README.md LICENSE config.example.toml src; do
  rm -rf "${APP_DIR:?}/$item"
  cp -a "$REPO/$item" "$APP_DIR/"
done
chown -R root:root "$APP_DIR"
chmod -R a+rX "$APP_DIR"

log "building virtualenv with uv"
# Keep any uv-managed interpreter under $APP_DIR so the service user can read it.
( cd "$APP_DIR" && UV_PYTHON_INSTALL_DIR="$APP_DIR/python" "$UV" sync --frozen --no-dev --no-editable )
chmod -R a+rX "$APP_DIR"

if [[ ! -f "$ETC_DIR/config.toml" ]]; then
  log "writing default config to $ETC_DIR/config.toml"
  install -o root -g "$SSA_USER" -m 0640 "$REPO/config.example.toml" "$ETC_DIR/config.toml"
else
  log "keeping existing $ETC_DIR/config.toml"
fi

PORT="$(awk -F'=' '/^[[:space:]]*port[[:space:]]*=/{gsub(/[^0-9]/,"",$2); print $2; exit}' "$ETC_DIR/config.toml")"
PORT="${PORT:-$SSA_PORT_DEFAULT}"
if ! systemctl is-active --quiet "$UNIT"; then
  require_port_free "$PORT"
fi

if [[ ! -f "$CRED_FILE" ]]; then
  log "generating registration token and encrypting it with systemd-creds"
  TOKEN="$(gen_token)"
  printf '%s' "$TOKEN" | systemd-creds encrypt --name=registration_token - "$CRED_FILE"
  chmod 0600 "$CRED_FILE"
  print_token_once "$TOKEN"
  unset TOKEN
else
  log "keeping existing encrypted token at $CRED_FILE (use rotate-token.sh to change it)"
fi

log "installing $UNIT"
install -o root -g root -m 0644 "$HERE/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"

IP="$(tailscale_ip)"; IP="${IP:-127.0.0.1}"
log "waiting for http://$IP:$PORT/health"
if wait_health "http://$IP:$PORT/health" 60; then
  log "service is up"
  print_reachability "$IP" "$PORT"
else
  warn "the service did not report healthy within 60s"
  echo "  journalctl -u $UNIT -n 50 --no-pager"
  echo "  tail -n 50 $LOG_DIR/aggregator.log"
  exit 1
fi
echo "  Logs:      $LOG_DIR/aggregator.log  (and: journalctl -u $UNIT -f)"
echo "  Config:    $ETC_DIR/config.toml  (edit, then: systemctl restart $UNIT)"
