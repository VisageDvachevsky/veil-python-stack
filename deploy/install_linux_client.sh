#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_PATH="${PROFILE_PATH:-}"
PROFILE_URL="${PROFILE_URL:-}"
PROFILE_TOKEN="${PROFILE_TOKEN:-}"

cd "$ROOT_DIR"
if [[ -n "$PROFILE_PATH" ]]; then
  python3 desktop/install_linux_client.py --profile "$PROFILE_PATH"
elif [[ -n "$PROFILE_URL" ]]; then
  python3 desktop/install_linux_client.py --profile-url "$PROFILE_URL"
elif [[ -n "$PROFILE_TOKEN" ]]; then
  python3 desktop/install_linux_client.py --profile-token "$PROFILE_TOKEN"
else
  python3 desktop/install_linux_client.py
fi

echo
echo "Client installed."
echo "Run: ~/.local/bin/veil-vpn-gui"
