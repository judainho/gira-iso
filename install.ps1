# Windows entry point for gira-iso.
#
# gira-iso builds images with QEMU/KVM, which only exist on Linux, so on Windows
# everything runs inside WSL2. This script makes sure WSL2 and an Ubuntu distro are
# present, copies the project into the distro's own filesystem (fast, and keeps file
# permissions) and runs install.sh there.
#
# Run from PowerShell:   powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = 'Stop'
$Distro = 'Ubuntu'
$Source = $PSScriptRoot

function Stop-WithMessage($Message) {
    Write-Host "error: $Message" -ForegroundColor Red
    exit 1
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Stop-WithMessage 'wsl.exe not found. Use Windows 10 2004+ or Windows 11 and enable WSL.'
}

# Is the distro installed? `wsl -l -q` prints UTF-16, so strip the NUL characters.
$installed = (& wsl.exe -l -q) -replace "`0", '' | Where-Object { $_.Trim() -ne '' }
if ($installed -notcontains $Distro) {
    Write-Host "==> Installing WSL2 with $Distro (needs an administrator prompt and may need a reboot)"
    & wsl.exe --install -d $Distro
    Write-Host ''
    Write-Host 'WSL was installed. Reboot if Windows asks, open Ubuntu once to create your' -ForegroundColor Yellow
    Write-Host 'user, then run this script again.' -ForegroundColor Yellow
    exit 0
}

Write-Host "==> Copying the project into $Distro (~/gira-iso)"
$wslSource = (& wsl.exe -d $Distro -- wslpath -a ($Source -replace '\\', '/')).Trim()
$copy = @"
set -e
mkdir -p "`$HOME/gira-iso"
cd '$wslSource'
tar --exclude=.venv --exclude=.git --exclude=builds --exclude=__pycache__ --exclude='*.qcow2' -cf - . \
  | tar -xf - -C "`$HOME/gira-iso"
cd "`$HOME/gira-iso"
sed -i 's/\r$//' install.sh scripts/*.sh
chmod +x install.sh scripts/*.sh
"@
& wsl.exe -d $Distro -- bash -lc $copy
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'could not copy the project into WSL' }

Write-Host "==> Running install.sh inside $Distro"
& wsl.exe -d $Distro -- bash -lc 'cd "$HOME/gira-iso" && ./install.sh'
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'install.sh failed, see the output above' }

Write-Host ''
Write-Host 'Done. From PowerShell or CMD run:   wsl -d Ubuntu -- gira-iso' -ForegroundColor Green
Write-Host 'Or open the Ubuntu terminal and run: gira-iso'
