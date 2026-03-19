$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$projectRoot = Resolve-Path (Join-Path $root "..\..")
$cmakeBuildDir = Join-Path $projectRoot "build\py-vs-vpn"
$distRoot = Join-Path $root "dist\windows-vpn"
$installerOutputDir = Join-Path $distRoot "installer"
$installerScript = Join-Path $PSScriptRoot "veil_vpn_client_windows.iss"
$wintunSource = $env:VEIL_WINTUN_DLL
$wintunVersion = "0.14.1"
$wintunUrl = "https://www.wintun.net/builds/wintun-$wintunVersion.zip"
$wintunZip = Join-Path $env:TEMP "wintun-$wintunVersion.zip"
$wintunExtract = Join-Path $env:TEMP "wintun-$wintunVersion"
$sodiumZip = Join-Path $env:TEMP "libsodium-1.0.19-stable-msvc.zip"
$sodiumUrl = "https://download.libsodium.org/libsodium/releases/libsodium-1.0.19-stable-msvc.zip"
Set-Location $root

function Assert-LastExitCode {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Step
  )

  if ($LASTEXITCODE -ne 0) {
    throw "$Step failed with exit code $LASTEXITCODE"
  }
}

python -m pip install -r desktop/requirements-desktop.txt
Assert-LastExitCode "pip install"

if (-not (Test-Path $sodiumZip)) {
  Invoke-WebRequest -Uri $sodiumUrl -OutFile $sodiumZip
}

if (Test-Path (Join-Path $projectRoot "CMakeLists.txt")) {
  $bindingsDirArg = "-DVEIL_PYTHON_BINDINGS_DIR:PATH=$root"
  $sodiumArchiveArg = "-DVEIL_SODIUM_PREBUILT_ARCHIVE:FILEPATH=$sodiumZip"
  cmake -S $projectRoot -B $cmakeBuildDir -G "Visual Studio 17 2022" -A x64 `
    -DVEIL_BUILD_PYTHON_BINDINGS=ON `
    $bindingsDirArg `
    $sodiumArchiveArg `
    -DVEIL_BUILD_TESTS=OFF `
    -DVEIL_ENABLE_SANITIZERS=OFF `
    -DVEIL_ENABLE_CLANG_TIDY=OFF `
    -DVEIL_ENABLE_MSVC_ANALYZE=OFF
  Assert-LastExitCode "cmake configure"
  cmake --build $cmakeBuildDir --config Release --target _veil_core_ext
  Assert-LastExitCode "cmake build _veil_core_ext"
}

$extensionSearchRoots = @(
  (Join-Path $root "veil_core"),
  (Join-Path $root "veil_core\\Release")
)

$extension = $null
foreach ($searchRoot in $extensionSearchRoots) {
  if (-not (Test-Path $searchRoot)) {
    continue
  }
  $extension = Get-ChildItem -Path $searchRoot -Filter "_veil_core_ext*.pyd" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($extension) {
    break
  }
}

if (-not $extension) {
  throw "Could not find compiled _veil_core_ext .pyd in $($extensionSearchRoots -join ', ') after CMake build"
}

New-Item -ItemType Directory -Force -Path $distRoot | Out-Null
Get-ChildItem -Force $distRoot -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne "installer" } | Remove-Item -Recurse -Force
New-Item -ItemType Directory -Force -Path $installerOutputDir | Out-Null
Get-ChildItem -Force $installerOutputDir -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force

pyinstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onedir `
  --uac-admin `
  --name veil-vpn-client `
  --paths $root `
  --collect-submodules veil_core `
  --collect-binaries veil_core `
  --add-binary "$($extension.FullName);veil_core" `
  --add-data "desktop/veil_vpn_client.example.json;." `
  desktop/veil_vpn_client.py
Assert-LastExitCode "pyinstaller veil-vpn-client"

pyinstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onedir `
  --uac-admin `
  --name veil-vpn-agent `
  --paths $root `
  --collect-submodules veil_core `
  --collect-binaries veil_core `
  --exclude-module PyQt6 `
  --exclude-module PyQt6.sip `
  --exclude-module veil_core.linux_client_app `
  --exclude-module veil_core.linux_proxy `
  --exclude-module veil_core.linux_server_app `
  --add-binary "$($extension.FullName);veil_core" `
  desktop/veil_vpn_agent.py
Assert-LastExitCode "pyinstaller veil-vpn-agent"

Copy-Item -Force -Recurse (Join-Path $root "dist\veil-vpn-client\*") $distRoot
Copy-Item -Force -Recurse (Join-Path $root "dist\veil-vpn-agent\*") $distRoot
Copy-Item -Force (Join-Path $PSScriptRoot "veil_vpn_client.example.json") (Join-Path $distRoot "veil_vpn_client.json")

if (-not $wintunSource) {
  if (-not (Test-Path $wintunZip)) {
    Invoke-WebRequest -Uri $wintunUrl -OutFile $wintunZip
  }
  if (Test-Path $wintunExtract) {
    Remove-Item -Recurse -Force $wintunExtract
  }
  Expand-Archive -Path $wintunZip -DestinationPath $wintunExtract -Force
  $wintunSource = Join-Path $wintunExtract "wintun\bin\amd64\wintun.dll"
}

if ($wintunSource -and (Test-Path $wintunSource)) {
  Copy-Item -Force $wintunSource (Join-Path $distRoot "wintun.dll")
} else {
  throw "Could not locate wintun.dll. Set VEIL_WINTUN_DLL or verify the downloaded archive layout."
}

$iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
if ($iscc) {
  & $iscc.Source /DSourceDir="$distRoot" $installerScript
  Assert-LastExitCode "ISCC"

  $installer = Get-ChildItem -Path $root, $PSScriptRoot -Recurse -Filter "VeilVPN-Setup-x64.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $installer) {
    throw "ISCC completed but VeilVPN-Setup-x64.exe was not found under $root or $PSScriptRoot"
  }

  if ($installer.DirectoryName -ne $installerOutputDir) {
    Copy-Item -Force $installer.FullName (Join-Path $installerOutputDir $installer.Name)
  }
}

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $distRoot\veil-vpn-client.exe"
Write-Host "  $distRoot\veil-vpn-agent.exe"
Write-Host "  $distRoot\veil_vpn_client.json"
if (Test-Path (Join-Path $distRoot "wintun.dll")) {
  Write-Host "  $distRoot\wintun.dll"
}
if (Test-Path (Join-Path $installerOutputDir "VeilVPN-Setup-x64.exe")) {
  Write-Host "  $installerOutputDir\VeilVPN-Setup-x64.exe"
}
Write-Host ""
Write-Host "If Inno Setup is installed, the script also produces a single setup executable."
