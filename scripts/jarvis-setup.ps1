<#
.SYNOPSIS
  JARVIS RD Assistant bootstrap (Phase 2 WS-2F) — Windows / PowerShell.

.DESCRIPTION
  Idempotent installer that mirrors scripts/jarvis-setup.sh:
    * Verifies Docker is available.
    * Generates a strong random .env from .env.example (only when .env is
      absent — never clobbers).
    * Skips mkcert by default (rare on Windows); local TLS remains optional.
    * Runs `docker compose up -d`.
    * Polls the direct dashboard HTTP URL with a 60s budget.
    * Prints the wizard URL.

  All user-facing config (SMTP, admin email, cloud LLM keys) lives in the
  web wizard — this script only handles Docker + secrets bootstrap.

.PARAMETER SkipMkcert
  Force-skip the mkcert detection branch even if mkcert.exe is on PATH.

.NOTES
  Requires PowerShell 5.1+ (built in to Windows 10/11). Run from the repo
  root or pass -WorkingDirectory.
#>
[CmdletBinding()]
param(
    [switch]$SkipMkcert,
    [string]$WorkingDirectory
)

$ErrorActionPreference = 'Stop'

# Resolve repo root (the directory this script lives in's parent).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = if ($WorkingDirectory) { $WorkingDirectory } else { Split-Path -Parent $ScriptDir }
Set-Location -Path $RepoRoot

function Write-Info  { param($Msg) Write-Host "[INFO]  $Msg" -ForegroundColor Cyan }
function Write-Ok    { param($Msg) Write-Host "[OK]    $Msg" -ForegroundColor Green }
function Write-Warn2 { param($Msg) Write-Host "[WARN]  $Msg" -ForegroundColor Yellow }
function Write-Err   { param($Msg) Write-Host "[ERROR] $Msg" -ForegroundColor Red }

Write-Host ""
Write-Host "================================================================" -ForegroundColor White
Write-Host "   JARVIS RD Assistant - bootstrap (WS-2F)                      " -ForegroundColor White
Write-Host "================================================================" -ForegroundColor White
Write-Host ""

# ---------------------------------------------------------------------------
# Prerequisite: Docker
# ---------------------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Err "Docker is not installed."
    Write-Host ""
    Write-Host "        Install Docker Desktop for Windows:"
    Write-Host "        https://www.docker.com/products/docker-desktop/"
    Write-Host ""
    exit 1
}
$dockerVersion = (docker --version) 2>$null
Write-Ok "Docker found: $dockerVersion"

try {
    $null = (docker compose version) 2>$null
    Write-Ok "docker compose plugin OK"
} catch {
    Write-Err "'docker compose' (V2 plugin) is missing. Update Docker Desktop."
    exit 1
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function New-RandomHex {
    param([int]$ByteCount = 32)
    $bytes = New-Object byte[] $ByteCount
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

function New-FernetKey {
    # Fernet wants exactly 32 random bytes encoded as urlsafe-base64.
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $b64 = [Convert]::ToBase64String($bytes)
    # urlsafe-base64 = base64 with '+' -> '-', '/' -> '_'. Pad/strip stays
    # the same (Fernet requires the trailing '=' padding, so we keep it).
    return ($b64 -replace '\+', '-' -replace '/', '_')
}

# ---------------------------------------------------------------------------
# .env generation (idempotent — never clobber)
# ---------------------------------------------------------------------------
$envFile     = Join-Path $RepoRoot '.env'
$envExample  = Join-Path $RepoRoot '.env.example'

if (Test-Path $envFile) {
    Write-Ok ".env already exists - leaving it alone"
} else {
    if (-not (Test-Path $envExample)) {
        Write-Err ".env.example missing; cannot bootstrap. Are you in the repo root?"
        exit 1
    }
    Write-Info "Generating .env from .env.example with strong random secrets"

    $secrets = @{
        'POSTGRES_PASSWORD'   = (New-RandomHex 32)
        'JARVIS_API_KEY'      = (New-RandomHex 32)
        'LITELLM_MASTER_KEY'  = (New-RandomHex 32)
        'JARVIS_CONFIG_KEY'   = (New-FernetKey)
    }

    $lines = Get-Content $envExample
    $out = foreach ($line in $lines) {
        $matched = $false
        foreach ($k in $secrets.Keys) {
            if ($line -eq "$k=") {
                "$k=$($secrets[$k])"
                $matched = $true
                break
            }
        }
        if (-not $matched) { $line }
    }
    Set-Content -Path $envFile -Value $out -Encoding ASCII
    Write-Ok "Generated .env with random secrets"
}

# ---------------------------------------------------------------------------
# init-dirs
# ---------------------------------------------------------------------------
$initDirs = Join-Path $ScriptDir 'init-dirs.sh'
if (Test-Path $initDirs) {
    Write-Info "Creating shared volume directories (via WSL bash if available)"
    try {
        $bashCmd = Get-Command bash -ErrorAction SilentlyContinue
        if ($bashCmd) { bash $initDirs }
    } catch { Write-Warn2 "init-dirs.sh failed (non-fatal): $_" }
}

# ---------------------------------------------------------------------------
# mkcert (best-effort; off by default on Windows)
# ---------------------------------------------------------------------------
if (-not $SkipMkcert -and (Get-Command mkcert -ErrorAction SilentlyContinue)) {
    Write-Info "mkcert detected - installing local CA + minting localhost cert"
    try {
        & mkcert -install | Out-Null
        $caddyDir = Join-Path $RepoRoot 'caddy/data'
        New-Item -ItemType Directory -Force -Path $caddyDir | Out-Null
        Push-Location $caddyDir
        try { & mkcert localhost 127.0.0.1 | Out-Null } finally { Pop-Location }
        Write-Ok "Local TLS via mkcert ready"
    } catch {
        Write-Warn2 "mkcert step failed (non-fatal): $_"
    }
} else {
    Write-Warn2 "mkcert not configured - HTTPS will use the self-signed cert."
    Write-Warn2 "Browsers will warn on first visit. Install mkcert for trusted local TLS:"
    Write-Warn2 "  https://github.com/FiloSottile/mkcert#installation"
}

# ---------------------------------------------------------------------------
# Bring stack up
# ---------------------------------------------------------------------------
$composeArgs = @('compose', '--env-file', '.env')
if (Test-Path (Join-Path $RepoRoot 'versions.env')) {
    $composeArgs += @('--env-file', 'versions.env')
}

# Detect already-running containers
try {
    $running = (docker @composeArgs ps --status running -q) 2>$null
    if ($running -and $running.Count -gt 0) {
        Write-Ok "Stack already has running containers - re-running 'up -d' (idempotent)"
    }
} catch {}

Write-Info "Starting Docker Compose stack"
docker @composeArgs up -d

# ---------------------------------------------------------------------------
# Wait for the dashboard to come up
# ---------------------------------------------------------------------------
$dashboardPort = '3001'
$envLine = Get-Content -Path (Join-Path $RepoRoot '.env') -ErrorAction SilentlyContinue |
    Where-Object { $_ -match '^DASHBOARD_HOST_PORT=' } |
    Select-Object -First 1
if ($envLine) {
    $dashboardPort = ($envLine -replace '^DASHBOARD_HOST_PORT=', '').Trim('"').Trim("'")
}
$dashboardUrl  = "http://localhost:$dashboardPort/"
$timeoutSecs   = 60
$intervalSecs  = 3
$elapsed       = 0
$ready         = $false

Write-Info "Waiting up to ${timeoutSecs}s for the dashboard to respond at $dashboardUrl"
while ($elapsed -lt $timeoutSecs) {
    try {
        $resp = Invoke-WebRequest -Uri $dashboardUrl -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        if ($resp.StatusCode -lt 500) { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds $intervalSecs
    $elapsed += $intervalSecs
}

if ($ready) {
    Write-Ok "Dashboard responded - JARVIS is up"
} else {
    Write-Warn2 "Dashboard did not respond within ${timeoutSecs}s."
    Write-Warn2 "Check 'docker compose logs -f dashboard' for boot diagnostics."
}

# ---------------------------------------------------------------------------
# Final pointer
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "JARVIS is starting. Open $dashboardUrl to finish setup." -ForegroundColor Green
Write-Host "The first-run web wizard will walk you through SMTP, the admin email,"
Write-Host "and (optionally) cloud LLM provider keys."
Write-Host "================================================================" -ForegroundColor Green
