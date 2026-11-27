# Larkyard one-command install for native Windows 10 / 11 (docs/spec.md section 3.2). In a normal PowerShell window:
#   irm https://raw.githubusercontent.com/3396720182-tech/larkyard/sanr/T0094-windows-native/scripts/windows/bootstrap.ps1 | iex
# Run the same line again to continue after fixing something, or to update: finished steps are skipped.
#
# This file only gets the prerequisites onto the machine, then hands over to ticketd\winsetup.py:
#   Python 3.9+, Git for Windows, Node 20.12+ (winget when missing; Git / Node may ask for admin once each)
#   npm -g: @larksuite/cli, lark-channel-bridge, @anthropic-ai/sandbox-runtime (only the missing ones)
#   Claude Code (official native installer), then git clone / fast-forward the repo
#   -> py -m ticketd.winsetup: Claude login, worker sandbox (one UAC), Feishu app (QR scan), workspace, ticketd service,
#      bridge service, owner, ticket doctor.
# Optional env: LARKYARD_BRANCH, LARKYARD_DIR (default ~\larkyard), LARKYARD_REPO, LARKYARD_OWNER (ou_...),
#   LARKYARD_ALLOW_LOOPBACK=1, LARK_CHANNEL_PROFILE, LARKYARD_WORKSPACE.
# Rules for this file: ASCII only (Windows PowerShell 5.1 reads BOM-less files in the ANSI code page); everything lives
# in a function and never calls exit, because `irm | iex` runs it inside the user's own session.

function Install-Larkyard {
  $ErrorActionPreference = 'Continue'  # native tools write progress to stderr; check $LASTEXITCODE instead
  $branch = if ($env:LARKYARD_BRANCH) { $env:LARKYARD_BRANCH } else { 'sanr/T0094-windows-native' }
  $repoUrl = if ($env:LARKYARD_REPO) { $env:LARKYARD_REPO } else { 'https://github.com/3396720182-tech/larkyard.git' }
  $dir = if ($env:LARKYARD_DIR) { $env:LARKYARD_DIR } else { Join-Path $HOME 'larkyard' }

  function Say($msg) { Write-Host "[larkyard] $msg" -ForegroundColor Cyan }
  function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

  function Update-SessionPath {
    $parts = @()
    foreach ($scope in 'Machine', 'User') {
      $v = [Environment]::GetEnvironmentVariable('Path', $scope)
      if ($v) { $parts += $v.Split(';') }
    }
    $parts += $env:Path.Split(';')
    $seen = @{}
    $out = @()
    foreach ($p in $parts) {
      if (-not $p) { continue }
      $k = $p.TrimEnd('\').ToLowerInvariant()
      if (-not $seen.ContainsKey($k)) { $seen[$k] = $true; $out += $p }
    }
    $env:Path = $out -join ';'
  }

  function Add-UserPath($d) {
    # Keep the registry value as REG_EXPAND_SZ so existing %VAR% entries still expand
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
      $raw = [string]$key.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
      $have = $raw.Split(';') | Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\').ToLowerInvariant() }
      if ($have -notcontains $d.TrimEnd('\').ToLowerInvariant()) {
        $new = if ($raw) { $raw.TrimEnd(';') + ';' + $d } else { $d }
        $key.SetValue('Path', $new, [Microsoft.Win32.RegistryValueKind]::ExpandString)
        # Tell Explorer so new terminals see it: setting a user variable through .NET broadcasts WM_SETTINGCHANGE
        [Environment]::SetEnvironmentVariable('LARKYARD_PATH_REFRESH', '1', 'User')
        [Environment]::SetEnvironmentVariable('LARKYARD_PATH_REFRESH', $null, 'User')
      }
    } finally { $key.Close() }
    if (($env:Path.Split(';') | ForEach-Object { $_.TrimEnd('\').ToLowerInvariant() }) -notcontains $d.TrimEnd('\').ToLowerInvariant()) {
      $env:Path = "$d;$env:Path"
    }
  }

  function Install-Winget($id, $what, $url) {
    if (-not (Have 'winget')) {
      throw "$what is missing and winget is not available on this PC. Install $what from $url (or ask IT), then run this command again."
    }
    Say "Installing $what with winget ($id). Click Yes if Windows asks for permission."
    & winget install -e --id $id --accept-package-agreements --accept-source-agreements
    Update-SessionPath
  }

  function Find-Python {
    $cands = @()
    if (Have 'py') {
      $p = & py -3 -c "import sys; print(sys.executable if sys.version_info >= (3, 9) else '')" 2>$null
      if ($p) { $cands += "$p".Trim() }
    }
    $c = Get-Command python -ErrorAction SilentlyContinue
    if ($c) { $cands += $c.Source }
    $cands += @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
      Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
    foreach ($x in $cands) {
      # Skip Microsoft Store Python (and its app aliases): it redirects registry / AppData writes into a private copy,
      # so the user PATH and TICKETD_PYTHON set by the installer would be invisible to everything else
      if ($x -and $x -notlike '*WindowsApps*' -and (Test-Path $x)) {
        $ok = & $x -c "import sys; print(int(sys.version_info >= (3, 9)))" 2>$null
        if ("$ok".Trim() -eq '1') { return $x }
      }
    }
    return $null
  }

  function Test-Node {
    if (-not (Have 'node')) { return $false }
    $v = & node -p "process.versions.node" 2>$null
    try { return ([version]"$v".Trim()) -ge [version]'20.12.0' } catch { return $false }
  }

  Say "Branch $branch -> $dir"
  if ([Environment]::Is64BitOperatingSystem -eq $false) { throw 'Claude Code needs 64-bit Windows.' }

  # 1. Python, Git, Node
  $py = Find-Python
  if (-not $py) {
    Install-Winget 'Python.Python.3.12' 'Python 3.12' 'https://www.python.org/downloads/windows/'
    $py = Find-Python
    if (-not $py) { throw 'Python 3.9+ still not found. Install it from https://www.python.org/downloads/windows/, then run this command again.' }
  }
  if (-not (Have 'git')) {
    Install-Winget 'Git.Git' 'Git for Windows' 'https://git-scm.com/download/win'
    if (-not (Have 'git')) { throw 'Git still not found. Install Git for Windows from https://git-scm.com/download/win, then run this command again.' }
  }
  if (-not (Test-Node)) {
    Install-Winget 'OpenJS.NodeJS.LTS' 'Node.js LTS' 'https://nodejs.org/'
    if (-not (Test-Node)) { throw 'Node.js 20.12+ still not found. Install the LTS from https://nodejs.org/, open a new PowerShell window and run this command again.' }
  }
  Say "python=$py"

  # 2. npm tools. npm.cmd, not npm: the npm.ps1 shim is blocked under the default execution policy
  $npmPrefix = "$(& npm.cmd prefix -g 2>$null)".Trim()
  if ($npmPrefix) { Add-UserPath $npmPrefix }
  $want = @()
  if (-not (Have 'lark-cli')) { $want += '@larksuite/cli' }
  if (-not (Have 'lark-channel-bridge')) { $want += 'lark-channel-bridge' }
  if (-not (Have 'srt')) { $want += '@anthropic-ai/sandbox-runtime' }
  if ($want.Count -gt 0) {
    Say "npm install -g $($want -join ' ')"
    & npm.cmd install -g @want
    if ($LASTEXITCODE -ne 0) { throw "npm install -g $($want -join ' ') failed (exit $LASTEXITCODE). Check the npm registry / proxy settings, then run this command again." }
    Update-SessionPath
  }

  # 3. Claude Code (native build). Its installer calls exit, so it runs in a child PowerShell
  if (-not (Have 'claude')) {
    Say 'Installing Claude Code (official installer)'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command 'irm https://claude.ai/install.ps1 | iex'
    $local = Join-Path $HOME '.local\bin'
    if (Test-Path (Join-Path $local 'claude.exe')) { Add-UserPath $local }
    Update-SessionPath
    if (-not (Have 'claude')) { throw 'Claude Code did not install. See https://code.claude.com/docs/en/setup, then run this command again.' }
  }

  # 4. Repo: clone, or fast-forward an existing clone
  if (Test-Path (Join-Path $dir '.git')) {
    Say "Updating $dir"
    & git -C $dir fetch -q origin $branch
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "Could not fetch $branch (offline?). Continuing with the code that is there."
    } else {
      $cur = "$(& git -C $dir rev-parse --abbrev-ref HEAD)".Trim()
      if ($cur -ne $branch) { & git -C $dir checkout -q $branch }
      if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not switch $dir to $branch (local changes?). Continuing with the code that is there."
      } else {
        & git -C $dir merge -q --ff-only FETCH_HEAD
        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not fast-forward $dir (local changes?). Continuing with the code that is there." }
      }
    }
  } elseif (Test-Path $dir) {
    throw "$dir exists but is not a git clone. Move it away, or set LARKYARD_DIR to another folder, then run this command again."
  } else {
    Say "Cloning into $dir"
    & git clone -q -b $branch $repoUrl $dir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed (exit $LASTEXITCODE)." }
  }

  # 5. The rest (prompts in Chinese) is ticketd\winsetup.py
  $saved = @{}
  foreach ($n in 'PYTHONUTF8', 'PYTHONIOENCODING', 'TICKETD_PYTHON') { $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }
  $env:PYTHONUTF8 = '1'
  $env:PYTHONIOENCODING = 'utf-8'
  $env:TICKETD_PYTHON = $py
  Push-Location $dir
  try {
    & $py -m ticketd.winsetup
    $rc = $LASTEXITCODE
  } finally {
    Pop-Location
    foreach ($n in $saved.Keys) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
    # The installer put the repo's bin on the user PATH: pick it up so `ticket ...` (e.g. the console's
    # `ticket enroll` line) works in this same window
    Update-SessionPath
  }
  if ($rc -ne 0) { Say "Not finished yet (exit $rc). Fix what is shown above, then run the same command again." }
}

try {
  Install-Larkyard
} catch {
  Write-Host "[larkyard] Stopped: $($_.Exception.Message)" -ForegroundColor Red
} finally {
  Remove-Item Function:\Install-Larkyard -ErrorAction SilentlyContinue
}
