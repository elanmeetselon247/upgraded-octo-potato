# deploy-windows.ps1 - install and start gituses on Windows.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\deploy-windows.ps1
#   powershell -ExecutionPolicy Bypass -File .\deploy-windows.ps1 -Stop
#   powershell -ExecutionPolicy Bypass -File .\deploy-windows.ps1 -Port 8083
#
# Safe to re-run: an existing store is kept, the server is restarted.

[CmdletBinding()]
param(
    [switch]$Stop,
    [int]$Port = 8082,
    [string]$StoreRoot = (Join-Path $HOME 'gituses'),
    [string]$RepoName = 'my-project'
)

$ErrorActionPreference = 'Stop'

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "    $message" -ForegroundColor Green }
function Write-Note($message) { Write-Host "    $message" -ForegroundColor Yellow }

function Fail($message, $hint) {
    Write-Host ""
    Write-Host "FAILED: $message" -ForegroundColor Red
    if ($hint) { Write-Host "        $hint" -ForegroundColor Red }
    exit 1
}

$PidFile = Join-Path $HOME 'gituses-serve.pid'
$LogFile = Join-Path $HOME 'gituses-serve.log'

# --- stopping ---------------------------------------------------------------
function Stop-Server {
    if (Test-Path $PidFile) {
        $old = Get-Content $PidFile -ErrorAction SilentlyContinue
        if ($old) {
            $proc = Get-Process -Id $old -ErrorAction SilentlyContinue
            if ($proc) {
                Stop-Process -Id $old -Force -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds 800
                Write-Ok "stopped server (pid $old)"
            }
        }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
    }
}

if ($Stop) {
    Write-Step "Stopping gituses"
    Stop-Server
    Write-Ok "stopped (or was not running)"
    exit 0
}

# --- prerequisites ----------------------------------------------------------
Write-Step "Checking prerequisites"

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Fail "git was not found on PATH." "Install git for Windows: https://git-scm.com/download/win then reopen PowerShell."
}
Write-Ok ("git {0}" -f ((& $git.Source --version) -replace '^git version ', '').Trim())

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $python) {
    Fail "python was not found on PATH." "Install Python 3.10+ from https://python.org and tick 'Add Python to PATH'."
}
$versionText = (& $python.Source --version 2>&1 | Out-String).Trim()
Write-Ok ("{0} ({1})" -f $versionText, $python.Source)

$backend = (& $git.Source --exec-path | Out-String).Trim()
if (-not $backend) {
    Fail "could not ask git where its helpers live." "Reinstall git for Windows."
}
$candidates = @(
    (Join-Path $backend 'git-http-backend.exe'),
    (Join-Path $backend 'git-http-backend')
)
if (-not ($candidates | Where-Object { Test-Path $_ })) {
    Fail "git-http-backend was not found in $backend." "The git install looks incomplete; reinstall git for Windows."
}
Write-Ok "git-http-backend found"

# --- install ----------------------------------------------------------------
Write-Step "Installing the gituses command"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& $python.Source -m pip install --quiet --no-build-isolation --user -e $here
if ($LASTEXITCODE -ne 0) {
    Fail "pip install failed." "Run it by hand to see the error: python -m pip install --user -e `"$here`""
}
$gituses = Get-Command gituses -ErrorAction SilentlyContinue
if (-not $gituses) {
    # pip --user puts scripts under the roaming profile; the Python version
    # is part of the path, so glob for it rather than guessing.
    $found = Get-ChildItem -Path (Join-Path $HOME 'AppData\Roaming\Python') `
                           -Filter 'gituses.exe' -Recurse -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($found) {
        $env:PATH = "$($found.DirectoryName);$env:PATH"
        $gituses = Get-Command $found.FullName
        Write-Note "added $($found.DirectoryName) to PATH for this session"
        Write-Note "to keep it, add that folder to your user PATH"
    } else {
        Fail "installed, but the gituses command is not on PATH." "Add Python's Scripts folder to PATH, then reopen PowerShell."
    }
}
Write-Ok (& $gituses.Source --version)

# --- store ------------------------------------------------------------------
Write-Step "Preparing the repository store at $StoreRoot"
$gu = $gituses.Source
if (Test-Path (Join-Path $StoreRoot 'gituses.json')) {
    Write-Ok "store already initialised, keeping it"
} else {
    & $gu init --root $StoreRoot | ForEach-Object { Write-Ok $_ }
}
$token = (& $gu token --root $StoreRoot).Trim()
if (-not $token) {
    Fail "the store has no token." "Run: gituses token --root `"$StoreRoot`" --rotate"
}
Write-Ok "token: $token"

Write-Step "Ensuring repository '$RepoName' exists"
$existing = & $gu list --root $StoreRoot
if ($existing -match "(^|\s)$([regex]::Escape($RepoName))(\s|$)") {
    Write-Ok "already exists"
} else {
    & $gu create --root $StoreRoot $RepoName --description 'created by deploy-windows.ps1' |
        ForEach-Object { Write-Ok $_ }
}

# --- start ------------------------------------------------------------------
Write-Step "Starting the server on port $Port"
Stop-Server

$arguments = @('-m', 'gituses', 'serve', '--root', $StoreRoot, '--host', '127.0.0.1', '--port', "$Port")
$proc = Start-Process -FilePath $python.Source -ArgumentList $arguments `
    -RedirectStandardOutput $LogFile -RedirectStandardError "$LogFile.err" `
    -WindowStyle Hidden -PassThru
$proc.Id | Set-Content $PidFile

$healthy = $false
foreach ($attempt in 1..20) {
    Start-Sleep -Milliseconds 400
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/_health" -TimeoutSec 2 -UseBasicParsing
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    if ($proc.HasExited) { break }
}
if (-not $healthy) {
    Write-Host ""
    foreach ($f in @($LogFile, "$LogFile.err")) {
        if (Test-Path $f) {
            Get-Content $f | Select-Object -Last 15 | ForEach-Object { Write-Host "  $_" }
        }
    }
    Fail "the server did not become healthy." "Read $LogFile (and $LogFile.err) for the reason."
}
Write-Ok "healthy, pid $($proc.Id)"

# --- done -------------------------------------------------------------------
$cloneUrl = "http://git:$token@127.0.0.1:$Port/$RepoName.git"
Write-Host ""
Write-Host "gituses is running." -ForegroundColor Green
Write-Host ""
Write-Host "  index:  http://127.0.0.1:$Port/"
Write-Host "  store:  $StoreRoot"
Write-Host "  token:  $token"
Write-Host ""
Write-Host "Next: paste this into the VS Code terminal" -ForegroundColor Cyan
Write-Host ""
Write-Host "  git clone $cloneUrl" -ForegroundColor White
Write-Host ""
Write-Host "Then File > Open Folder and pick '$RepoName'."
Write-Host "Stop it later with: .\deploy-windows.ps1 -Stop"

