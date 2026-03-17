[Setup]
AppName=Veil Chat Client
AppVersion=0.1.0
DefaultDirName={autopf}\VeilChatClient
DefaultGroupName=Veil Chat Client
OutputDir=dist\installer
OutputBaseFilename=veil-chat-client-setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "dist\veil-chat-client.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "desktop\veil_chat_client.example.json"; DestDir: "{app}"; DestName: "veil_chat_client.json"; Flags: ignoreversion onlyifdoesntexist

[Icons]
Name: "{group}\Veil Chat Client"; Filename: "{app}\veil-chat-client.exe"
Name: "{autodesktop}\Veil Chat Client"; Filename: "{app}\veil-chat-client.exe"

[Run]
Filename: "{app}\veil-chat-client.exe"; Description: "Launch Veil Chat Client"; Flags: nowait postinstall skipifsilent
