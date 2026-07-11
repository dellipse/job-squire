# Job Squire — bootstrap (Windows)
#
# The one command that lands the `job-squire` CLI and hands off to it. This
# script installs the CLI and nothing else -- every step after this is a
# `job-squire` subcommand (see docs/PLAN-deployment-modes.md Section 6).
#
# Usage (PowerShell):
#   irm https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.ps1 | iex
#
# Pin a specific version instead of the latest release:
#   $env:JOBSQUIRE_VERSION = "0.6.0"
#   irm https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.ps1 | iex
#
# Integrity: the requested version is resolved through the GitHub Releases
# API (never a bare branch), then that release's tag is resolved to an
# immutable commit SHA with `git ls-remote` before anything is installed.
# The CLI is installed with `pip install ... @ git+https://...@<sha>`, so
# pip/git fetch exactly that commit -- git's object store is content-addressed
# (every tree/blob/commit is verified against its own hash as part of the
# clone), and the fetch itself runs over HTTPS/TLS. No separate checksum or
# signature file is published for the CLI today (unlike the Docker image,
# which is cosign-signed in .github/workflows/ci.yml) -- pinning to the
# resolved commit SHA rather than the mutable tag name is the integrity
# mechanism here: what gets installed cannot silently change after the
# version-resolution step above, even if the tag is later moved.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Repo = 'dellipse/job-squire'
$GitUrl = "https://github.com/$Repo.git"
$Api = "https://api.github.com/repos/$Repo"
$InstallDir = if ($env:JOBSQUIRE_INSTALL_DIR) { $env:JOBSQUIRE_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'job-squire' }
$VenvDir = Join-Path $InstallDir 'cli'
$BinDir = Join-Path $VenvDir 'Scripts'

function Write-Info { param([string]$Msg) Write-Host "-> $Msg" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Msg) Write-Host "OK $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "! $Msg" -ForegroundColor Yellow }
function Die {
    param([string]$Msg)
    Write-Host "x $Msg" -ForegroundColor Red
    exit 1
}

function Invoke-NativeOrDie {
    # Native executables (git, pip) signal failure via $LASTEXITCODE, not a
    # terminating exception, even with $ErrorActionPreference = 'Stop'.
    param([string]$Exe, [string[]]$CmdArgs)
    & $Exe @CmdArgs
    if ($LASTEXITCODE -ne 0) {
        Die "Command failed (exit $LASTEXITCODE): $Exe $($CmdArgs -join ' ')"
    }
}

# ── Prerequisites ─────────────────────────────────────────────────────────
# Deliberately not auto-installed: the bootstrap installs the CLI and
# nothing else (see the file header). Runtime (Podman/Docker) install-with-
# consent is the CLI's own job (Prompt C3), not this script's.
$GitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $GitCmd) {
    Die "'git' is required but wasn't found on PATH. Install Git for Windows (https://git-scm.com/download/win or 'winget install Git.Git') and re-run this command."
}

$PyExe = $null
$PyPrefixArgs = @()
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
$pyCmd = Get-Command py -ErrorAction SilentlyContinue
if ($pythonCmd) {
    $PyExe = $pythonCmd.Source
} elseif ($pyCmd) {
    $PyExe = $pyCmd.Source
    $PyPrefixArgs = @('-3')
} else {
    Die "Python 3.11+ is required but wasn't found on PATH (tried 'python' and 'py'). Install it from https://www.python.org/downloads/windows/ or 'winget install Python.Python.3.12', then re-run this command."
}

$pyOk = & $PyExe @PyPrefixArgs -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)'
if ($LASTEXITCODE -ne 0 -or $pyOk -ne '1') {
    $pyVer = & $PyExe @PyPrefixArgs --version 2>&1
    Die "Python 3.11+ is required (found: $pyVer). Install a newer Python 3 and re-run this command."
}

# ── GitHub API helper ────────────────────────────────────────────────────
function Invoke-GhApi {
    param([string]$Url)
    $headers = @{
        'Accept'     = 'application/vnd.github+json'
        'User-Agent' = 'job-squire-bootstrap'
    }
    if ($env:GITHUB_TOKEN) { $headers['Authorization'] = "Bearer $($env:GITHUB_TOKEN)" }
    try {
        $resp = Invoke-WebRequest -Uri $Url -Headers $headers -UseBasicParsing -TimeoutSec 10
        return [PSCustomObject]@{ StatusCode = [int]$resp.StatusCode; Body = $resp.Content }
    } catch {
        $status = 0
        if ($_.Exception.PSObject.Properties.Name -contains 'Response' -and $_.Exception.Response) {
            try { $status = [int]$_.Exception.Response.StatusCode } catch { $status = 0 }
        }
        return [PSCustomObject]@{ StatusCode = $status; Body = $null }
    }
}

# ── Resolve the requested version to a release tag ──────────────────────
$tag = $null
if ($env:JOBSQUIRE_VERSION) {
    $ver = $env:JOBSQUIRE_VERSION -replace '^v', ''
    $tag = "v$ver"
    Write-Info "Looking up release $tag ..."
    $r = Invoke-GhApi "$Api/releases/tags/$tag"
    if ($r.StatusCode -eq 200) {
        # found
    } elseif ($r.StatusCode -eq 404) {
        Die "JOBSQUIRE_VERSION=$($env:JOBSQUIRE_VERSION) does not match a published release (looked for tag '$tag'). See https://github.com/$Repo/releases for available versions. Nothing was installed."
    } else {
        Die "Could not reach the GitHub releases API to verify JOBSQUIRE_VERSION=$($env:JOBSQUIRE_VERSION) (HTTP $($r.StatusCode)). Check your network connection and try again. Nothing was installed."
    }
} else {
    Write-Info "Looking up the latest job-squire release ..."
    $r = Invoke-GhApi "$Api/releases/latest"
    if ($r.StatusCode -eq 200) {
        $tag = ($r.Body | ConvertFrom-Json).tag_name
    } elseif ($r.StatusCode -eq 404) {
        # /releases/latest only ever returns a non-prerelease, non-draft
        # release, and can 404 even when releases exist (e.g. everything so
        # far is a pre-release, as during this project's early phase). Fall
        # back to the most recently published release of any kind rather
        # than leaving the default path with nothing to install.
        $r2 = Invoke-GhApi "$Api/releases"
        if ($r2.StatusCode -ne 200) {
            Die "Could not reach the GitHub releases API (HTTP $($r2.StatusCode)). Check your network connection and try again. Nothing was installed."
        }
        $releases = @($r2.Body | ConvertFrom-Json)
        if ($releases.Count -eq 0) {
            Die "No releases have been published yet at https://github.com/$Repo/releases. Nothing to install -- try again later, or pin a version with `$env:JOBSQUIRE_VERSION once one exists."
        }
        $tag = $releases[0].tag_name
        if ($releases[0].prerelease) {
            Write-Warn "Latest published release ($tag) is a pre-release; installing it since no stable release exists yet."
        }
    } else {
        Die "Could not reach the GitHub releases API (HTTP $($r.StatusCode)). Check your network connection and try again. Nothing was installed."
    }
    if (-not $tag) {
        Die "GitHub returned a release with no tag_name -- this shouldn't happen. See https://github.com/$Repo/releases."
    }
}

Write-Ok "Target version: $tag"

# ── Pin the tag to an immutable commit before installing anything ───────
Write-Info "Resolving $tag to a commit ..."
$lsRemoteOut = & git ls-remote $GitUrl "refs/tags/$tag" "refs/tags/$tag^{}" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $lsRemoteOut) {
    Die "Could not resolve tag '$tag' to a commit via 'git ls-remote'. Nothing was installed."
}
# @(...) must wrap the whole pipeline, not just $lsRemoteOut on the input
# side -- a Where-Object result with exactly one match collapses to a bare
# scalar on assignment otherwise, and indexing a *string* with [-1] returns
# its last character rather than the last array element.
$lsLines = @($lsRemoteOut | Where-Object { $_ -ne '' })
$sha = ($lsLines[-1] -split "`t")[0]
if (-not $sha) {
    Die "Could not resolve tag '$tag' to a commit via 'git ls-remote'. Nothing was installed."
}
Write-Ok "Pinned to commit $sha"

# ── Install into an isolated environment ─────────────────────────────────
# A dedicated venv rather than a machine-wide `pip install` keeps the CLI's
# dependencies from ever colliding with anything else on the machine. Safe
# to re-run: reuses the venv if present.
$venvPython = Join-Path $BinDir 'python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Info "Creating an isolated environment at $VenvDir ..."
    Invoke-NativeOrDie $PyExe ($PyPrefixArgs + @('-m', 'venv', $VenvDir))
}
$venvPip = Join-Path $BinDir 'pip.exe'
Invoke-NativeOrDie $venvPip @('install', '--quiet', '--upgrade', 'pip')
Write-Info "Installing job-squire-cli ($tag) ..."
$pkgSpec = "job-squire-cli[query] @ git+$GitUrl@$sha#subdirectory=job_squire_cli"
Invoke-NativeOrDie $venvPip @('install', '--quiet', '--upgrade', $pkgSpec)
Write-Ok "Installed to $BinDir"

# ── Put job-squire on PATH for future sessions ───────────────────────────
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }
$pathEntries = $userPath -split ';' | Where-Object { $_ -ne '' }
if ($pathEntries -notcontains $BinDir) {
    $newUserPath = if ($userPath.Trim() -eq '') { $BinDir } else { "$userPath;$BinDir" }
    [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
    Write-Ok "Added $BinDir to your User PATH"
}
$env:Path = "$BinDir;$env:Path"

# ── Launch ─────────────────────────────────────────────────────────────
$jobSquireExe = Join-Path $BinDir 'job-squire.exe'
$isInteractive = [Environment]::UserInteractive -and -not ([Console]::IsOutputRedirected)
if ($isInteractive) {
    Write-Info "Launching job-squire ..."
    & $jobSquireExe create
    exit $LASTEXITCODE
} else {
    Write-Ok "job-squire installed at $jobSquireExe"
    Write-Host ""
    Write-Host "Open a new terminal (so PATH picks up job-squire), then run:"
    Write-Host "    job-squire create"
}
