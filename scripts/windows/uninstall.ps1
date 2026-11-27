# Uninstall the ticketd service on native Windows (docs/spec.md section 3.2).
# Usage (PowerShell, in the repo):  powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall.ps1
# Optional env: TICKETD_PYTHON (Python 3.9+ python.exe), TICKETD_HOME, TICKETD_LABEL,
#               LARK_CHANNEL_HOME / LARK_CHANNEL_PROFILE (bridge profile).
# All the work is done by ticketd\wininstall.py; this file only finds Python. Kept ASCII on purpose:
# Windows PowerShell 5.1 reads BOM-less scripts in the ANSI code page.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$py = $env:TICKETD_PYTHON
if (-not $py) {
  $launcher = Get-Command py -ErrorAction SilentlyContinue
  if ($launcher) { $py = (& py -3 -c 'import sys; print(sys.executable)').Trim() }
}
if (-not $py) {
  $cand = Get-Command python -ErrorAction SilentlyContinue
  if ($cand -and $cand.Source -notlike '*WindowsApps*') { $py = $cand.Source }
}
if (-not $py -or -not (Test-Path $py)) {
  Write-Error 'Python 3.9+ not found. Install it from https://www.python.org/downloads/windows/ or set TICKETD_PYTHON.'
  exit 1
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
# Python writes warnings to stderr; Windows PowerShell 5.1 would turn them into terminating errors under 'Stop'
$ErrorActionPreference = 'Continue'
Push-Location $repo
try {
  & $py -m ticketd.wininstall uninstall
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
