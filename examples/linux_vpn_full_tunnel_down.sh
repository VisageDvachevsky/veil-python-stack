#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="/run/veil-vpn"
TUN_NAME="${TUN_NAME:-veilfull0}"
CONFIG_PATH="${CONFIG_PATH:-$STATE_DIR/${TUN_NAME}.client.json}"

cd "$ROOT_DIR"
if [[ ! -f "$CONFIG_PATH" ]]; then
  python3 desktop/veil_vpn_ctl.py --config "$CONFIG_PATH" save-config --tun-name "$TUN_NAME" >/dev/null
fi

python3 desktop/veil_vpn_ctl.py --config "$CONFIG_PATH" down
rm -f "$CONFIG_PATH"
