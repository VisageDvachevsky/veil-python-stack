$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

python -m pip install -r desktop/requirements-desktop.txt

pyinstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onefile `
  --name veil-chat-client `
  --add-data "desktop/veil_chat_client.example.json;." `
  desktop/veil_chat_client.py

Write-Host ""
Write-Host "Build complete:"
Write-Host "  dist/veil-chat-client.exe"
Write-Host ""
Write-Host "Copy veil_chat_client.example.json next to the exe and rename it to veil_chat_client.json"
