#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLIC_HOST="${PUBLIC_HOST:-}"
PUBLIC_INTERFACE="${PUBLIC_INTERFACE:-}"
PROFILE_OUT="${PROFILE_OUT:-}"

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "PUBLIC_HOST is required"
  exit 1
fi

cd "$ROOT_DIR"
if [[ -n "$PUBLIC_INTERFACE" ]]; then
  python3 desktop/veil_vpn_server_ctl.py init --public-host "$PUBLIC_HOST" --public-interface "$PUBLIC_INTERFACE"
else
  python3 desktop/veil_vpn_server_ctl.py init --public-host "$PUBLIC_HOST"
fi
python3 desktop/veil_vpn_server_ctl.py install
python3 desktop/veil_vpn_server_ctl.py enable-service

echo
echo "Server installed."
if [[ -n "$PROFILE_OUT" ]]; then
  python3 desktop/veil_vpn_server_ctl.py write-client-profile --output "$PROFILE_OUT"
  echo "Client profile written to: $PROFILE_OUT"
else
  echo "Client profile:"
  python3 desktop/veil_vpn_server_ctl.py export-client-profile
fi
echo
echo "Client token:"
python3 desktop/veil_vpn_server_ctl.py export-client-token
