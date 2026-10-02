<#
.SYNOPSIS
  swarm one-shot installer for Windows (PowerShell 5.1 or 7). Mirrors install.sh.

.DESCRIPTION
  For each detected host (claude and/or codex): add or update the swarm marketplace, install the
  plugin, activate it (Claude: enable; Codex: print the manual /hooks trust step), run
  `swarm bootstrap --host <h>` with the freshly installed plugin's own bin\swarm.cmd, then
  `swarm migrate` and `swarm doctor`. Every step is idempotent: re-running is safe.

  Installs for the current user only (no all-users mode: it never needs or uses administrator
  rights). Everything lives under %USERPROFILE%: ~\.local\bin\swarm.cmd (the launcher), and
  ~\.local\share\swarm (venv, host files); the config is ~\.config\swarm\config.toml.

  Run it:
    irm https://raw.githubusercontent.com/fcarucci/Swarm/main/install.ps1 -OutFile install.ps1
    .\install.ps1 [-Target claude|codex|both] [-Channel release|main | -Main] [-Ref <tag|branch>] ...
  or in one line, with flags:
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/fcarucci/Swarm/main/install.ps1))) -Main

.PARAMETER Target
  claude, codex or both (alias -Host). Default: every claude/codex CLI found on PATH and in the
  usual install locations.
.PARAMETER Marketplace
  Marketplace to add (default https://github.com/fcarucci/Swarm.git). A local path is a frozen
  tree, for testing before a push: nothing is pinned.
.PARAMETER Channel
  release (default): the newest vX.Y.Z tag. main: the tip of main. With no tag found, or when
  git ls-remote fails, falls back to main with a warning.
.PARAMETER Main
  Shorthand for -Channel main.
.PARAMETER Ref
  Install exactly this tag or branch (overrides -Channel).
.PARAMETER Yes
  Never prompt; assume yes (required when input is redirected).
.PARAMETER Force
  Pass --force to `swarm migrate`, so a stale local swarm-job marker doesn't block it.
.PARAMETER NoPath
  Don't add ~\.local\bin to your user PATH.
.PARAMETER NoColor
  Never colour the output (also off when NO_COLOR is set).
#>
[CmdletBinding()]
param(
  [Alias('Host')][ValidateSet('claude', 'codex', 'both')][string]$Target,
  [string]$Marketplace = 'https://github.com/fcarucci/Swarm.git',
  [ValidateSet('release', 'main')][string]$Channel = 'release',
  [switch]$Main,
  [string]$Ref,
  [switch]$Yes,
  [switch]$Force,
  [switch]$NoPath,
  [switch]$NoColor
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$MarketplaceName = 'swarm'
$PluginSpec = 'swarm@swarm'
$MinPy = [version]'3.11'
$ChannelGiven = $PSBoundParameters.ContainsKey('Channel') -or $Main.IsPresent
if ($Main) { $Channel = 'main' }
if ($Ref -and $Ref -notmatch '^[A-Za-z0-9._/+][A-Za-z0-9._/+-]*$') { throw "-Ref must be a tag or branch name (got: $Ref)" }
$UseColor = (-not $NoColor) -and (-not $env:NO_COLOR) -and (-not [Console]::IsOutputRedirected)
$Script:PinRef = ''
$Script:PinActive = $false
$Script:ConfigNeedsAttention = $false
$Script:DoctorFailed = $false
$Script:Status = [ordered]@{}
$HomeDir = [Environment]::GetFolderPath('UserProfile')
$Tmp = Join-Path ([IO.Path]::GetTempPath()) ("swarm-install-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $Tmp | Out-Null

function Paint([string]$text, [string]$code) { if ($UseColor) { "$([char]27)[${code}m$text$([char]27)[0m" } else { $text } }
function Log([string]$m) { Write-Host ("{0} {1}" -f (Paint '==>' '1;34'), $m) }
function Warn([string]$m) { [Console]::Error.WriteLine(("{0} {1}" -f (Paint 'swarm-install: warning:' '33'), $m)) }
function Die([string]$m) { throw "swarm-install: $m" }

# Runs a native command, never throws on a non-zero exit; returns @{Code; Out}. stdin is closed.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
  $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try {
    $out = @() | & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }   # @() | : stdin closed
    $code = $LASTEXITCODE
  } catch { $out = @("$_"); $code = 127 } finally { $ErrorActionPreference = $prev }
  if ($null -eq $code) { $code = 0 }
  return @{ Code = [int]$code; Out = (($out | Out-String).TrimEnd()) }
}

function Show-Steps([string]$text) {
  foreach ($line in ($text -split "`r?`n")) {
    if ($UseColor -and $line -match '(^|\s)(OK|ok)(\s|$)') { $line = $line -replace '\b(OK|ok)\b', (Paint '$1' '32') }
    elseif ($UseColor -and $line -match '\b(FAIL|failed)\b') { $line = $line -replace '\b(FAIL|failed)\b', (Paint '$1' '31') }
    elseif ($UseColor -and $line -match '\b(WARN|manual|skipped|refused)\b') { $line = $line -replace '\b(WARN|manual|skipped|refused)\b', (Paint '$1' '33') }
    Write-Host $line
  }
}

function Confirm-Step([string]$prompt) {
  if ($Yes) { return $true }
  if ([Console]::IsInputRedirected) { Die 'no terminal to ask on: pass -Yes to proceed without prompting' }
  $r = Read-Host "$prompt [y/N]"
  return $r -match '^(y|yes)$'
}

# ---------------------------------------------------------------- hosts

function Find-HostBin([string]$name) {
  $cmd = Get-Command $name -CommandType Application, ExternalScript -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($cmd) { return $cmd.Source }
  $extra = @(
    (Join-Path $HomeDir ".local\bin\$name.exe"), (Join-Path $HomeDir ".claude\local\$name.exe"),
    (Join-Path $env:APPDATA "npm\$name.cmd"), (Join-Path $HomeDir "AppData\Local\Programs\$name\$name.exe"))
  foreach ($p in $extra) { if ($p -and (Test-Path -LiteralPath $p)) { return $p } }
  return $null
}

$Bins = @{}
$Hosts = @()
foreach ($h in @('claude', 'codex')) {
  if ($Target -and $Target -ne 'both' -and $Target -ne $h) { continue }
  $b = Find-HostBin $h
  if ($b) { $Bins[$h] = $b; $Hosts += $h }
  elseif ($Target -eq $h) { Die "--host $h given, but no '$h' CLI found on PATH or in the usual install locations" }
}
if ($Hosts.Count -eq 0) { Die 'no claude or codex CLI found on PATH or in common install locations; install one of them first, or pass -Target claude|codex|both' }
Log ("hosts: " + ($Hosts -join ' '))
Log "marketplace: $Marketplace"

# ---------------------------------------------------------------- preflight

function Find-Python {
  foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
    $c = Get-Command $cand[0] -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { continue }
    $r = Invoke-Native $c.Source (@($cand | Select-Object -Skip 1) + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])'))
    if ($r.Code -eq 0 -and $r.Out -match '^\d+\.\d+$' -and [version]$r.Out -ge $MinPy) { return @{ Exe = $c.Source; Version = $r.Out } }
  }
  return $null
}

$git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $git) { Die 'git not found on PATH (needed by the CLIs, and to find the newest release tag)' }
$py = Find-Python
if (-not $py) { Die "Python $MinPy or newer not found on PATH (try: winget install Python.Python.3.12); swarm reads its config with tomllib" }
Log "preflight: python $($py.Version), $((& git --version) 2>&1)"

# ---------------------------------------------------------------- channel

function Resolve-Channel {
  $Script:PinRef = ''; $Script:PinActive = $false
  if (Test-Path -LiteralPath $Marketplace -PathType Container) {
    Log "channel: the marketplace is a local path ($Marketplace): installing it as is"
    return
  }
  $Script:PinActive = $true
  if ($Ref) { $Script:PinRef = $Ref; Log "channel: ref $Ref (explicit -Ref)"; return }
  if ($Channel -eq 'main') { Log 'channel: main (tip of main)'; return }
  $env:GIT_TERMINAL_PROMPT = '0'
  $r = Invoke-Native 'git' @('ls-remote', '--tags', '--refs', '--sort=-v:refname', $Marketplace, 'v*')
  $tag = $null
  if ($r.Code -eq 0) {
    foreach ($line in ($r.Out -split "`r?`n")) {
      if ($line -match 'refs/tags/(v\d+\.\d+\.\d+)\s*$') { $tag = $Matches[1]; break }
    }
  }
  if ($tag) { $Script:PinRef = $tag; Log "channel: release (newest tag $tag)" }
  else {
    Warn "no release tag (vX.Y.Z) found at $Marketplace, or git ls-remote failed: falling back to the tip of main"
    $Script:ResolvedChannel = 'main'
    Log 'channel: main (tip of main, fallback)'
  }
}
$Script:ResolvedChannel = $Channel
Resolve-Channel

# ---------------------------------------------------------------- marketplace / plugin

function Add-Marketplace([string]$h) {
  $bin = $Bins[$h]
  $src = $Marketplace
  $extra = @()
  if ($Script:PinRef) { if ($h -eq 'claude') { $src = "$Marketplace#$($Script:PinRef)" } else { $extra = @('--ref', $Script:PinRef) } }
  Log "[$h] marketplace: add/update $src"
  $r = Invoke-Native $bin (@('plugin', 'marketplace', 'add', $src) + $extra)
  if ($r.Code -eq 0) { Log "[$h] marketplace added"; return }
  $logs = @($r.Out)
  if ($Script:PinActive) {   # already registered, maybe at another ref: only remove+add moves it
    Invoke-Native $bin @('plugin', 'marketplace', 'remove', $MarketplaceName) | Out-Null
    $r2 = Invoke-Native $bin (@('plugin', 'marketplace', 'add', $src) + $extra)
    if ($r2.Code -eq 0) { Log "[$h] marketplace re-added at $(if ($Script:PinRef) { $Script:PinRef } else { 'the tip of main' })"; return }
    $logs += $r2.Out
  }
  $verbs = if ($h -eq 'codex') { @('upgrade', 'update') } else { @('update') }
  foreach ($v in $verbs) {
    $r3 = Invoke-Native $bin @('plugin', 'marketplace', $v, $MarketplaceName)
    if ($r3.Code -eq 0) { Log "[$h] marketplace already present; ${v}d"; return }
    $logs += $r3.Out
  }
  Invoke-Native $bin @('plugin', 'marketplace', 'remove', $MarketplaceName) | Out-Null
  $r4 = Invoke-Native $bin (@('plugin', 'marketplace', 'add', $src) + $extra)
  if ($r4.Code -eq 0) { Log "[$h] marketplace re-added (remove+add) after add/update failed"; return }
  $logs += $r4.Out
  [Console]::Error.WriteLine(($logs -join "`n"))
  Die "[$h] could not add or update the marketplace $Marketplace (see output above)"
}

function Install-Plugin([string]$h) {
  $bin = $Bins[$h]
  $sub = if ($h -eq 'claude') { 'install' } else { 'add' }
  Log "[$h] plugin $sub $PluginSpec"
  $r = Invoke-Native $bin @('plugin', $sub, $PluginSpec)
  if ($r.Code -ne 0) {
    $l = Invoke-Native $bin @('plugin', 'list')
    if ($l.Out -notmatch [regex]::Escape($PluginSpec)) {
      [Console]::Error.WriteLine($r.Out)
      $Script:Status[$h] = 'install failed'
      Die "[$h] plugin $sub $PluginSpec failed and it is not listed as installed (see output above)"
    }
    Log "[$h] plugin $PluginSpec already installed"
  } else { Log "[$h] plugin $PluginSpec installed/updated" }
}

function Enable-Plugin([string]$h) {
  if ($h -eq 'claude') {
    Invoke-Native $Bins[$h] @('plugin', 'enable', $PluginSpec) | Out-Null
    $l = Invoke-Native $Bins[$h] @('plugin', 'list')
    if ($l.Out -match [regex]::Escape($PluginSpec)) { Log "[$h] plugin enabled (doctor confirms hooks are actually active)" }
    else { Warn "[$h] plugin $PluginSpec not listed after enable attempt; check '$($Bins[$h]) plugin list'" }
  } else {
    Log "[$h] activation needs the manual /hooks trust step (see the note printed at the end)"
  }
}

# ---------------------------------------------------------------- locate the installed plugin

function Get-PluginVersion([string]$root) {
  foreach ($m in @('.claude-plugin\plugin.json', '.codex-plugin\plugin.json')) {
    try { return [string]((Get-Content -Raw -LiteralPath (Join-Path $root $m) | ConvertFrom-Json).version) } catch { }
  }
  return $null
}

function ConvertTo-Ver([string]$v) { try { [version]($v -replace '[^0-9.].*$', '') } catch { [version]'0.0' } }

function Find-PluginRoot([string]$h) {
  $reported = @(); $cacheOnly = @()
  $Script:Looked = ''
  if ($h -eq 'claude') {
    $ccd = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HomeDir '.claude' }
    try {
      $d = Get-Content -Raw -LiteralPath (Join-Path $ccd 'plugins\installed_plugins.json') | ConvertFrom-Json
      foreach ($i in @($d.plugins.$PluginSpec)) { if ($i -and $i.installPath) { $reported += [string]$i.installPath } }
    } catch { }
    $cacheOnly = @(Get-ChildItem -Path (Join-Path $ccd 'plugins\cache\swarm\swarm\*') -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName)
  } else {
    $chd = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HomeDir '.codex' }
    $cacheOnly = @(Get-ChildItem -Path (Join-Path $chd 'plugins\cache\swarm\swarm\*') -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName)
    $r = Invoke-Native $Bins[$h] @('plugin', 'list', '--json')
    $iv = $null
    if ($r.Code -eq 0) {
      try {
        foreach ($p in @((ConvertFrom-Json $r.Out).installed)) { if ($p.pluginId -like 'swarm@*' -and $p.version) { $iv = [string]$p.version } }
      } catch { }
    }
    if ($iv) { $reported = @($cacheOnly | Where-Object { (Get-PluginVersion $_) -eq $iv }); $cacheOnly = @($cacheOnly | Where-Object { $reported -notcontains $_ }) }
  }
  $Script:Looked = "reported: $($reported -join ', '); cache: $($cacheOnly -join ', ')"
  foreach ($set in @($reported, $cacheOnly)) {
    $ok = @($set | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $_ 'bin\swarm.cmd')) })
    if ($ok.Count -gt 0) { return ($ok | Sort-Object { ConvertTo-Ver (Get-PluginVersion $_) } | Select-Object -Last 1) }
  }
  if ((Test-Path -LiteralPath $Marketplace -PathType Container) -and (Test-Path -LiteralPath (Join-Path $Marketplace 'bin\swarm.cmd'))) { return (Resolve-Path -LiteralPath $Marketplace).Path }
  return $null
}

# ---------------------------------------------------------------- bootstrap / migrate / doctor

function Invoke-Swarm([string]$sw, [string[]]$Arguments, [hashtable]$Env = @{}) {
  $saved = @{}
  foreach ($k in $Env.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, $Env[$k]) }
  try { return Invoke-Native $sw $Arguments } finally { foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) } }
}

function Invoke-Bootstrap([string]$h, [string]$sw) {
  Log "[$h] swarm bootstrap --host $h"
  $chan = if ($ChannelGiven -and -not $Ref -and $Script:PinActive) { $Script:ResolvedChannel } else { '' }
  $r = Invoke-Swarm $sw @('bootstrap', '--host', $h) @{ SWARM_CHANNEL = $chan; SWARM_NO_MIGRATE = '1' }
  Show-Steps $r.Out
  if ($r.Code -ne 0) { Die "[$h] swarm bootstrap failed (see output above)" }
  if ($r.Out -match '(?m)^config .*manual') { $Script:ConfigNeedsAttention = $true; $Script:ConfigDetail = (($r.Out -split "`r?`n") | Where-Object { $_ -match '^config ' } | Select-Object -First 1) }
}

function Invoke-Migrate([string]$sw) {
  Log 'swarm migrate'
  $r = Invoke-Native $sw @('migrate')
  Show-Steps $r.Out
  if ($r.Code -eq 0) { return }
  if ($r.Out -notmatch 'active on this machine') { Die 'swarm migrate failed (see output above)' }
  if (-not $Force) { Die "swarm migrate refused: a swarm job is active on this machine (see above). This installer does not force through it: wait for the job to finish, re-run with -Force to override stale markers, or run 'swarm migrate --force' by hand." }
  Warn 'forcing migrate over the active job marker(s) above: double check they are stale'
  Log 'swarm migrate --force'
  $r2 = Invoke-Native $sw @('migrate', '--force')
  Show-Steps $r2.Out
  if ($r2.Code -ne 0) { Die 'swarm migrate --force failed (see output above)' }
}

function Invoke-Doctor([string]$h, [string]$sw) {
  Log "[$h] swarm doctor --host $h"
  $r = Invoke-Native $sw @('doctor', '--host', $h)
  Show-Steps $r.Out
  if ($r.Code -ne 0) { $Script:DoctorFailed = $true; Warn "[$h] swarm doctor reported at least one FAIL (see above)"; $Script:Status[$h] = 'doctor FAIL' }
  else { $Script:Status[$h] = 'ok' }
}

function Add-UserPath {
  $bin = Join-Path $HomeDir '.local\bin'
  $user = [Environment]::GetEnvironmentVariable('Path', 'User')
  if ($user -and (($user -split ';') | Where-Object { $_.TrimEnd('\') -ieq $bin })) { return }
  if ($NoPath) { Warn "$bin is not on your PATH: add it to run 'swarm' from any terminal"; return }
  [Environment]::SetEnvironmentVariable('Path', $(if ($user) { "$user;$bin" } else { $bin }), 'User')
  Log "added $bin to your user PATH (open a new terminal to pick it up)"
}

function Write-CodexNotes {
  @'

Codex: manual step required
----------------------------
Codex runs no plugin hook until its hooks are trusted, and it only reads ~\.codex\config.toml at
session start:

  1. Start an interactive `codex` session, run /hooks, and trust the swarm plugin's hooks.
  2. Start one more NEW Codex session (not the one you trusted from): that is the one whose
     SessionStart hook actually runs, and the one that picks up the config.toml changes
     `swarm bootstrap` already made (writable roots, agents.max_depth).
  3. From inside that new session (or a shell), check: swarm doctor --host codex
'@ | Write-Host
}

# ---------------------------------------------------------------- main

try {
  if (-not (Confirm-Step "Install/update/activate swarm for: $($Hosts -join ' ') from $Marketplace ?")) { Die 'aborted (not confirmed)' }
  foreach ($h in $Hosts) { Add-Marketplace $h; Install-Plugin $h; Enable-Plugin $h }

  $roots = @{}
  foreach ($h in $Hosts) {
    $root = Find-PluginRoot $h
    if (-not $root) { Die "[$h] can't find the installed swarm plugin's bin\swarm.cmd in $h's plugin cache; check '$($Bins[$h]) plugin list' ($($Script:Looked)), then re-run install.ps1" }
    $roots[$h] = $root
    $pv = Get-PluginVersion $root
    Log "[$h] using $root\bin\swarm.cmd"
    Log "[$h] installed swarm $(if ($pv) { $pv } else { 'unknown' }): channel $($Script:ResolvedChannel), ref $(if ($Script:PinRef) { $Script:PinRef } else { 'main (tip)' })"
    Invoke-Bootstrap $h (Join-Path $root 'bin\swarm.cmd')
  }
  Add-UserPath

  if ($Script:ConfigNeedsAttention) {
    Log 'board config needs your attention before migrate/doctor can do anything useful:'
    Write-Host $Script:ConfigDetail
    Log 'Fix ~\.config\swarm\config.toml (or $env:SWARM_CONFIG) yourself, then re-run install.ps1 (idempotent) to run migrate and doctor.'
    if ($Hosts -contains 'codex') { Write-CodexNotes }
    return
  }

  Invoke-Migrate (Join-Path $roots[$Hosts[0]] 'bin\swarm.cmd')
  foreach ($h in $Hosts) { Invoke-Doctor $h (Join-Path $roots[$h] 'bin\swarm.cmd') }
  if ($Hosts -contains 'codex') { Write-CodexNotes }
  Log 'swarm 0.1.0 and later move the board schema forward: if the board is shared with other hosts, upgrade all of them together (docs\REFERENCE.md, "Upgrading").'
  foreach ($k in $Script:Status.Keys) { Log "$k : $($Script:Status[$k])" }
  if ($Script:DoctorFailed) { Die 'swarm doctor reported at least one FAIL above; fix it, then re-run install.ps1 (idempotent)' }
  Log 'done.'
} finally {
  Remove-Item -Recurse -Force -LiteralPath $Tmp -ErrorAction SilentlyContinue
}
