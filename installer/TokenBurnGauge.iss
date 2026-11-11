; Token Burn Gauge by soybean_alien
; Per-user installer (no administrator rights). Built by tools\build-installer.ps1, which passes the version.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define Root ".."

[Setup]
AppId={{6E2B6C0C-5A0F-4C59-9B1D-7D2A5C1E8F31}
AppName=Token Burn Gauge
AppVersion={#AppVersion}
AppPublisher=soybean_alien
DefaultDirName={localappdata}\Programs\TokenBurnGauge
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir={#Root}\artifacts
OutputBaseFilename=TokenBurnGauge-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Token Burn Gauge
LicenseFile={#Root}\LICENSE

[Files]
Source: "{#Root}\artifacts\publish\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "{#Root}\agents.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\collector_b.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\proxy.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\tap.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\tap_switch.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\start.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\tap-off.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\gauge.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\config.example.json"; DestDir: "{app}"; Flags: ignoreversion
; Settings are yours: written once, never overwritten, kept on uninstall.
Source: "{#Root}\config.example.json"; DestDir: "{app}"; DestName: "config.json"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#Root}\tools\stop-collectors.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Token Burn Gauge"; Filename: "{app}\TokenGauge.exe"

[Run]
Filename: "{app}\TokenGauge.exe"; Description: "Start Token Burn Gauge"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop this copy's app and collectors so their files can be removed.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\stop-collectors.ps1"""; Flags: runhidden; RunOnceId: "StopCollectors"
