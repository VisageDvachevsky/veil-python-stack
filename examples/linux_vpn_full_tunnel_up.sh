#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="/run/veil-vpn"
mkdir -p "$STATE_DIR"

TUN_NAME="${TUN_NAME:-veilfull0}"
CONFIG_PATH="${CONFIG_PATH:-$STATE_DIR/${TUN_NAME}.client.json}"

SERVER_HOST="${SERVER_HOST:-185.23.35.241}"
SERVER_PORT="${SERVER_PORT:-4433}"
CLIENT_NAME="${CLIENT_NAME:-client}"
PSK_HEX="${PSK_HEX:-abababababababababababababababababababababababababababababababab}"
TUN_ADDR="${TUN_ADDR:-10.200.0.2/30}"
TUN_PEER="${TUN_PEER:-10.200.0.1}"
PACKET_MTU="${PACKET_MTU:-1300}"
KEEPALIVE_INTERVAL="${KEEPALIVE_INTERVAL:-10}"
KEEPALIVE_TIMEOUT="${KEEPALIVE_TIMEOUT:-30}"
PROTOCOL_WRAPPER="${PROTOCOL_WRAPPER:-none}"
PERSONA_PRESET="${PERSONA_PRESET:-custom}"

cd "$ROOT_DIR"
python3 desktop/veil_vpn_ctl.py --config "$CONFIG_PATH" save-config \
  --server-host "$SERVER_HOST" \
  --server-port "$SERVER_PORT" \
  --client-name "$CLIENT_NAME" \
  --psk-hex "$PSK_HEX" \
  --tun-name "$TUN_NAME" \
  --tun-address "$TUN_ADDR" \
  --tun-peer "$TUN_PEER" \
  --packet-mtu "$PACKET_MTU" \
  --keepalive-interval "$KEEPALIVE_INTERVAL" \
  --keepalive-timeout "$KEEPALIVE_TIMEOUT" \
  --protocol-wrapper "$PROTOCOL_WRAPPER" \
  --persona-preset "$PERSONA_PRESET" >/dev/null

python3 desktop/veil_vpn_ctl.py --config "$CONFIG_PATH" up
