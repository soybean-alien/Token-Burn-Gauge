# Token Burn Gauge by soybean_alien
# Builds the app and the installer:  powershell -ExecutionPolicy Bypass -File tools\build-installer.ps1
# Needs the .NET 10 SDK and Inno Setup 6. Output: artifacts\TokenBurnGauge-Setup-<version>.exe and its SHA-256.
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."
$proj = Join-Path $root "app\TokenGauge\TokenGauge.csproj"
$version = ([xml](Get-Content $proj)).Project.PropertyGroup.Version | Select-Object -First 1
$publish = Join-Path $root "artifacts\publish"
if (Test-Path $publish) { Remove-Item $publish -Recurse -Force }
dotnet publish $proj -c Release -o $publish --nologo
if ($LASTEXITCODE -ne 0) { throw "Publish failed" }
Get-ChildItem $publish -Filter *.pdb | Remove-Item -Force
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $iscc "/DAppVersion=$version" (Join-Path $root "installer\TokenBurnGauge.iss") | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) { throw "Installer build failed" }
$setup = Join-Path $root "artifacts\TokenBurnGauge-Setup-$version.exe"
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText((Join-Path $root "artifacts\SHA256SUMS-$version.txt"), "$hash  TokenBurnGauge-Setup-$version.exe`n")
Write-Host "Installer: $setup"
