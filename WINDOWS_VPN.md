# Windows VPN Client

This document describes the Windows-native Veil VPN packaging and runtime flow.

## Deliverable

Users download a single installer from GitHub Releases:

- `VeilVPN-Setup-x64.exe`

The installer lays down:

- `veil-vpn-client.exe`
- `veil-vpn-agent.exe`
- `veil_vpn_client.json`
- `wintun.dll`

## Architecture

### GUI

Entry point:

- `desktop/veil_vpn_client.py`

Responsibilities:

- import profile JSON
- import `veil://profile/...` token
- edit connection settings
- trigger connect/disconnect
- show status and logs

### Agent

Entry point:

- `desktop/veil_vpn_agent.py`

Responsibilities:

- keep the Veil session alive
- reconnect when configured
- create/open the Wintun adapter
- configure IPv4 and routes
- bridge packets between Wintun and `VpnConnection`

### Runtime

Runtime helpers live in:

- `veil_core/windows_client_app.py`
- `veil_core/windows_wintun.py`

## Build

From the submodule root:

```powershell
pwsh -File desktop/build_windows_vpn_client.ps1
```

Artifacts land in:

```text
dist/windows-vpn/
dist/windows-vpn/installer/
```

## Provisioning

Windows uses the same provisioning contract as Linux:

- `ClientConnectionProfile`
- JSON profile import
- `veil://profile/...` token import

That contract lives in:

- `veil_core/provisioning.py`

## Live Validation Checklist

1. Install `VeilVPN-Setup-x64.exe` on Windows 10/11 x64.
2. Launch the GUI.
3. Import a profile JSON or token.
4. Click `Connect`.
5. Confirm the Wintun adapter exists and the status switches to connected.
6. Confirm routes are present and traffic reaches the Veil server.
7. Click `Disconnect`.
8. Confirm routes are removed and the adapter session is torn down cleanly.
