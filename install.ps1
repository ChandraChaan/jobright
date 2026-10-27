# jobright installer for Windows (PowerShell).
#   irm https://raw.githubusercontent.com/ChandraChaan/jobright/main/install.ps1 | iex
# Installs into %USERPROFILE%\.jobright\app, adds a `jobright` command, and
# opens the setup wizard in your browser. Re-run to update.
$ErrorActionPreference = "Stop"

$Repo = if ($env:JOBRIGHT_REPO) { $env:JOBRIGHT_REPO } else { "https://github.com/ChandraChaan/jobright" }
$Ref = if ($env:JOBRIGHT_REF) { $env:JOBRIGHT_REF } else { "main" }
$HomeDir = if ($env:JOBRIGHT_HOME) { $env:JOBRIGHT_HOME } else { Join-Path $env:USERPROFILE ".jobright" }
$App = Join-Path $HomeDir "app"
$Bin = Join-Path $HomeDir "bin"

function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }

# 1. Python 3.11+
$Py = $null
foreach ($c in @("py -3.13", "py -3.12", "py -3.11", "python")) {
  try {
    $parts = $c.Split(" ")
    & $parts[0] $parts[1..9] -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $Py = $parts; break }
  } catch {}
}
if (-not $Py) {
  Write-Host "Python 3.11+ is needed. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." -ForegroundColor Red
  exit 1
}

# 2. Its own environment
New-Item -ItemType Directory -Force -Path $App, $Bin | Out-Null
$VenvPy = Join-Path $App "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
  Say "Creating environment in $App"
  & $Py[0] $Py[1..9] -m venv (Join-Path $App "venv")
}
& $VenvPy -m pip install --quiet --upgrade pip
Say "Installing jobright ($Ref)"
& $VenvPy -m pip install --quiet --upgrade "jobright @ git+$Repo@$Ref"

# 3. Browser engine
Say "Installing the browser engine (one-time, ~150 MB)"
& $VenvPy -m playwright install chromium | Out-Null

# 4. A `jobright` command
$Cmd = Join-Path $Bin "jobright.cmd"
Set-Content -Path $Cmd -Value "@echo off`r`nset JOBRIGHT_HOME=$HomeDir`r`n`"$VenvPy`" -m jobright %*"
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$Bin*") {
  [Environment]::SetEnvironmentVariable("Path", "$UserPath;$Bin", "User")
  $env:Path = "$env:Path;$Bin"
}

Say "Installed. Opening setup..."
& $Cmd start
Write-Host ""
Write-Host "  jobright is ready."
Write-Host "    Open it any time:   jobright start   (in a new terminal)"
Write-Host "    Check the install:  jobright doctor"
Write-Host "    Your data:          $HomeDir (stays on this computer)"
