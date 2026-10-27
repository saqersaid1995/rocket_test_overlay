# SFT STELLAR - Windows installer
# Installs (or repairs) SFT STELLAR for the current user:
#   Git + Python (via winget if missing) -> clone from GitHub -> Python environment
#   -> config.yaml -> Desktop and Start Menu shortcuts with the SFT STELLAR icon.
# Safe to run again: it repairs and updates an existing install.

$ErrorActionPreference = 'Stop'
$Repo    = 'https://github.com/saqersaid1995/rocket_test_overlay.git'
$Branch  = 'teststand/console'
$AppName = 'SFT STELLAR'

function Step($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "    $t" -ForegroundColor Green }
function Fail($t) { Write-Host "`n[X] $t" -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Find-Python {
    foreach ($c in @(@('py', '-3'), @('python'))) {
        try {
            $exe = $c[0]; $args2 = @($c | Select-Object -Skip 1)
            $v = & $exe @args2 -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $parts = $v.Trim().Split('.')
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) { return ,$c }
            }
        } catch { }
    }
    return $null
}

Write-Host "============================================" -ForegroundColor DarkCyan
Write-Host "   $AppName  -  Installer"                   -ForegroundColor White
Write-Host "   Stellar Kinetics test-stand console"      -ForegroundColor DarkGray
Write-Host "============================================" -ForegroundColor DarkCyan

# ---- where to install --------------------------------------------------------
$ScriptRoot = Split-Path -Parent $PSScriptRoot
if (Test-Path (Join-Path $ScriptRoot 'teststand\app.py')) {
    $Target = $ScriptRoot                                   # running from inside an existing copy
} else {
    $Target = Join-Path $env:USERPROFILE $AppName
}
Ok "Install folder: $Target"

# ---- Git ---------------------------------------------------------------------
Step 'Checking Git'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host '    Git not found - installing with winget...'
    try { winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements | Out-Host } catch { }
    Refresh-Path
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        $g = 'C:\Program Files\Git\cmd'; if (Test-Path $g) { $env:Path += ";$g" }
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail 'Git could not be installed. Install it from https://git-scm.com and run this again.' }
}
Ok (git --version)

# ---- Python ------------------------------------------------------------------
Step 'Checking Python 3.10+'
$Py = Find-Python
if (-not $Py) {
    Write-Host '    Python not found - installing Python 3.12 with winget...'
    try { winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements | Out-Host } catch { }
    Refresh-Path
    $Py = Find-Python
    if (-not $Py) { Fail 'Python could not be installed. Install Python 3.12 from https://python.org and run this again.' }
}
$PyExe = $Py[0]; $PyArgs = @($Py | Select-Object -Skip 1)
Ok (& $PyExe @PyArgs --version)

# ---- Get the code ------------------------------------------------------------
Step 'Getting SFT STELLAR from GitHub'
if (Test-Path (Join-Path $Target '.git')) {
    git -C $Target fetch origin $Branch
    if ($LASTEXITCODE -ne 0) { Fail 'Could not reach GitHub. Check the internet connection / GitHub sign-in.' }
    git -C $Target checkout $Branch | Out-Null
    git -C $Target merge --ff-only "origin/$Branch" | Out-Host
    Ok 'Existing install updated'
} elseif (Test-Path (Join-Path $Target 'teststand\app.py')) {
    Ok 'Using the files in this folder (not a Git copy: the update button will not work)'
} else {
    Write-Host '    A GitHub sign-in window may open the first time - sign in with your GitHub account.'
    git clone --branch $Branch --single-branch $Repo $Target
    if ($LASTEXITCODE -ne 0) { Fail 'Download from GitHub failed. Check the internet connection and GitHub sign-in.' }
    Ok 'Downloaded'
}
Set-Location $Target

# ---- Python environment ------------------------------------------------------
Step 'Setting up the Python environment (first time takes a few minutes)'
$Venv = Join-Path $Target '.venv'
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    & $PyExe @PyArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail 'Could not create the Python environment.' }
}
$VPy  = Join-Path $Venv 'Scripts\python.exe'
$VPyw = Join-Path $Venv 'Scripts\pythonw.exe'
& $VPy -m pip install --upgrade pip -q --disable-pip-version-check | Out-Null
& $VPy -m pip install -r (Join-Path $Target 'requirements.txt') -q --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail 'Installing Python packages failed. Check the internet connection.' }
Ok 'Python packages installed'

# ---- Settings ----------------------------------------------------------------
Step 'Settings'
$Cfg = Join-Path $Target 'config.yaml'
if (-not (Test-Path $Cfg)) {
    Copy-Item (Join-Path $Target 'config.example.yaml') $Cfg
    Ok 'Created config.yaml (ignition starts in SIMULATION mode)'
} else {
    Ok 'Kept your existing config.yaml'
}

# ---- Shortcuts ---------------------------------------------------------------
Step 'Creating shortcuts'
$Icon = Join-Path $Target 'assets\sft_stellar.ico'
$Shell = New-Object -ComObject WScript.Shell
$Places = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) $AppName)
)
foreach ($dir in $Places) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $lnk = $Shell.CreateShortcut((Join-Path $dir "$AppName.lnk"))
    $lnk.TargetPath       = $VPyw
    $lnk.Arguments        = "`"$(Join-Path $Target 'launcher.pyw')`""
    $lnk.WorkingDirectory = $Target
    $lnk.IconLocation     = "$Icon,0"
    $lnk.Description      = 'SFT STELLAR - Stellar Kinetics test-stand console'
    $lnk.Save()
    Ok "$dir\$AppName.lnk"
}
$cfgLnk = $Shell.CreateShortcut((Join-Path $Places[1] "$AppName settings.lnk"))
$cfgLnk.TargetPath = 'notepad.exe'; $cfgLnk.Arguments = "`"$Cfg`""; $cfgLnk.Save()

# ---- Firewall (the ESP32 connects to this PC on TCP 9100) ---------------------
Step 'Firewall rule for the ESP32 (TCP 9100)'
$rule = Get-NetFirewallRule -DisplayName "$AppName telemetry" -ErrorAction SilentlyContinue
if ($rule) {
    Ok 'Already present'
} else {
    Write-Host '    Windows will ask for permission (UAC) to open port 9100 for the ESP32.'
    $cmd = "New-NetFirewallRule -DisplayName '$AppName telemetry' -Direction Inbound -Protocol TCP -LocalPort 9100 -Action Allow -Profile Any | Out-Null"
    try {
        Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile', '-Command', $cmd
        Ok 'Port 9100 opened'
    } catch {
        Write-Host '    Skipped. If the ESP32 cannot connect, allow Python when Windows asks.' -ForegroundColor Yellow
    }
}

Write-Host "`n============================================" -ForegroundColor DarkCyan
Write-Host "   $AppName installed." -ForegroundColor Green
Write-Host "   Open it from the Desktop icon or the Start Menu."
Write-Host "============================================`n" -ForegroundColor DarkCyan
$open = Read-Host 'Open SFT STELLAR now? (Y/n)'
if ($open -ne 'n') { Start-Process $VPyw -ArgumentList "`"$(Join-Path $Target 'launcher.pyw')`"" -WorkingDirectory $Target }
