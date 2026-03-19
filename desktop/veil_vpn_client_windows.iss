[Setup]
AppName=Veil VPN
AppVersion=0.1.0
DefaultDirName={autopf}\VeilVPN
DefaultGroupName=Veil VPN
OutputDir=dist\windows-vpn\installer
OutputBaseFilename=VeilVPN-Setup-x64
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "installer\\*"
Source: "{#SourceDir}\veil_vpn_client.json"; DestDir: "{commonappdata}\VeilVPN"; DestName: "client.json"; Flags: onlyifdoesntexist

[Icons]
Name: "{group}\Veil VPN"; Filename: "{app}\veil-vpn-client.exe"
Name: "{autodesktop}\Veil VPN"; Filename: "{app}\veil-vpn-client.exe"

[Run]
Filename: "{app}\veil-vpn-client.exe"; Description: "Launch Veil VPN"; Flags: nowait postinstall skipifsilent
