#!/usr/bin/env bash
# One-command install for the Service Status Aggregator.
#
#   git clone https://github.com/clams2121/service_status_aggregator.git
#   cd service_status_aggregator
#   sudo ./install.sh              # systemd service (recommended on the OpenClaw host)
#
#   ./install.sh --venv            # no root: run from this checkout with deploy/venv/run.sh
#   sudo ./install.sh --docker     # container with host networking
#
# The script checks prerequisites, offers to install uv if it is missing, then hands
# off to the matching installer under deploy/. Re-running it upgrades in place.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/common.sh
source "$HERE/deploy/common.sh"

MODE=""
for arg in "$@"; do
  case "$arg" in
    --systemd) MODE=systemd ;;
    --venv)    MODE=venv ;;
    --docker)  MODE=docker ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) die "unknown option: $arg (use --systemd, --venv or --docker)" ;;
  esac
done

# Pick a sensible default: systemd when root on a systemd machine, otherwise a venv.
if [[ -z "$MODE" ]]; then
  if [[ $EUID -eq 0 ]] && command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    MODE=systemd
  else
    MODE=venv
  fi
  log "no mode given; using --$MODE"
fi

case "$MODE" in
  systemd|docker) need_root ;;
  venv) [[ $EUID -ne 0 ]] || warn "running the venv install as root; consider a normal user" ;;
esac

# Prerequisites common to every path.
need_cmd curl "sudo apt install curl"
need_cmd python3 "sudo apt install python3"
if [[ "$MODE" == "docker" ]]; then
  need_cmd docker "https://docs.docker.com/engine/install/ubuntu/"
fi

# uv builds the virtualenv (systemd and venv paths). Offer the official installer, never silently.
ensure_uv() {
  if command -v uv >/dev/null 2>&1; then return 0; fi
  for candidate in "$HOME/.local/bin/uv" /root/.local/bin/uv /usr/local/bin/uv \
                   "${SUDO_USER:+/home/$SUDO_USER/.local/bin/uv}"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      local dir
      dir="$(dirname "$candidate")"
      export PATH="$dir:$PATH"
      return 0
    fi
  done
  echo
  echo "uv (https://docs.astral.sh/uv/) is required to build the Python environment."
  echo "It can be installed now with the official installer:"
  echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
  read -r -p "Install uv now? [y/N] " answer
  case "$answer" in
    y|Y|yes|YES)
      curl -LsSf https://astral.sh/uv/install.sh | sh
      export PATH="$HOME/.local/bin:$PATH"
      command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; open a new shell and re-run"
      ;;
    *) die "install uv, then re-run ./install.sh" ;;
  esac
}

case "$MODE" in
  systemd)
    ensure_uv
    log "installing as a systemd service"
    exec "$HERE/deploy/systemd/install.sh"
    ;;
  venv)
    ensure_uv
    log "installing into a virtualenv in this checkout"
    exec "$HERE/deploy/venv/install.sh"
    ;;
  docker)
    log "installing as a Docker container"
    exec "$HERE/deploy/docker/install.sh"
    ;;
esac
