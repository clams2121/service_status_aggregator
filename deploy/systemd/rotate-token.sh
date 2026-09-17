#!/usr/bin/env bash
# Generate a new registration token, re-encrypt it, restart the service.
# Afterwards update every registering service with the new token.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common.sh
source "$HERE/../common.sh"

CRED_FILE=/etc/credstore.encrypted/ssa-registration_token
UNIT=service-status-aggregator.service

need_root
need_cmd systemd-creds

if [[ -n "${1:-}" ]]; then
  TOKEN="$1"
  (( ${#TOKEN} >= 16 )) || die "token must be at least 16 characters"
else
  TOKEN="$(gen_token)"
fi
[[ -f "$CRED_FILE" ]] && cp -a "$CRED_FILE" "$CRED_FILE.previous"
printf '%s' "$TOKEN" | systemd-creds encrypt --name=registration_token - "$CRED_FILE"
chmod 0600 "$CRED_FILE"
systemctl restart "$UNIT"
print_token_once "$TOKEN"
echo "Previous encrypted token kept at $CRED_FILE.previous; delete it once every service is updated."
