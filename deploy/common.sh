#!/usr/bin/env bash
# Shared helpers for the install scripts. Source this file; do not run it.

SSA_USER="${SSA_USER:-svcstatus}"
# shellcheck disable=SC2034  # used by the install scripts that source this file
SSA_PORT_DEFAULT=8720

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

need_root() {
  [[ $EUID -eq 0 ]] || die "run this script as root (sudo)"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1${2:+ ($2)}"
}

# Create a dedicated, non-login system user if missing. Prints nothing.
ensure_user() {
  if ! id -u "$SSA_USER" >/dev/null 2>&1; then
    log "creating system user $SSA_USER"
    useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$SSA_USER"
  fi
}

# ensure_dir PATH OWNER MODE
ensure_dir() {
  mkdir -p "$1"
  chown "$2" "$1"
  chmod "$3" "$1"
}

# 64 hex characters of randomness for the registration token.
gen_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  fi
}

print_token_once() {
  cat <<MSG

  ------------------------------------------------------------------
  Registration token (shown ONCE; it is not stored in plain text here):

      $1

  Store it in your password manager now. Every service that registers
  with the aggregator needs it as:  Authorization: Bearer <token>
  ------------------------------------------------------------------

MSG
}

# The Tailscale IPv4 of this machine, or empty.
tailscale_ip() {
  if command -v tailscale >/dev/null 2>&1; then
    tailscale ip -4 2>/dev/null | head -n1
  elif command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show 2>/dev/null | awk '$4 ~ /^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\./ {split($4,a,"/"); print a[1]; exit}'
  fi
}

# port_free PORT -> 0 if nothing is listening on PORT (any address)
port_free() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ! ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .
  else
    ! (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null
  fi
}

require_port_free() {
  local port="$1"
  if ! port_free "$port"; then
    warn "something is already listening on port $port:"
    ss -ltnp "sport = :$port" 2>/dev/null || true
    die "choose another port in the config ([server].port) and re-run"
  fi
}

# wait_health URL SECONDS -> 0 when /health returns HTTP 200
wait_health() {
  local url="$1" deadline=$(( $(date +%s) + ${2:-60} ))
  while (( $(date +%s) < deadline )); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# Print where the dashboard is reachable, given the bind ip.
print_reachability() {
  local ip="$1" port="$2"
  if [[ "$ip" == "127.0.0.1" ]]; then
    warn "no Tailscale IP was found; the service is bound to 127.0.0.1 only"
    echo "  Reach it through an SSH tunnel:  ssh -L $port:127.0.0.1:$port $(hostname)"
  else
    echo "  Dashboard: http://$ip:$port/"
    echo "  Health:    http://$ip:$port/health"
    echo "  Register:  POST http://$ip:$port/register"
  fi
}
