<#
.SYNOPSIS
    Install wizard for gerber2laser on Windows.

.DESCRIPTION
    Walks through the install one question at a time: where to put it, whether
    to make shortcuts, and whether to register the KiCad plugin. It creates a
    private virtualenv and installs PySide6 + gerbonara into it, so nothing is
    added to your system Python. KiCad itself is never touched or installed.

    Run it from the repo:
        powershell -ExecutionPolicy Bypass -File install\windows\install.ps1

.PARAMETER Yes
    Take the default answer to every question (unattended install).
.PARAMETER InstallDir
    Where to install. Default: %LOCALAPPDATA%\Programs\gerber2laser
.PARAMETER NoShortcuts
    Skip the Desktop and Start Menu shortcuts.
.PARAMETER NoKiCadPlugin
    Skip registering the pcbnew action plugin.
.PARAMETER DesktopDir
.PARAMETER StartMenuDir
.PARAMETER KiCadPluginDir
    Override where those things go. Mostly for testing the installer.
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [string]$InstallDir,
    [switch]$NoShortcuts,
    [switch]$NoKiCadPlugin,
    [string]$DesktopDir,
    [string]$StartMenuDir,
    [string]$KiCadPluginDir
)

$ErrorActionPreference = 'Stop'
$Source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$AppName = 'gerber2laser'

function Step($n, $text) {
    Write-Host ''
    Write-Host "[$n] $text" -ForegroundColor Cyan
}
function Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }
function Ask($question, $default) {
    if ($Yes) { return $default }
    $a = Read-Host "    $question [$default]"
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return $a.Trim()
}
function AskYN($question, [bool]$default = $true) {
    if ($Yes) { return $default }
    $d = if ($default) { 'Y/n' } else { 'y/N' }
    $a = Read-Host "    $question [$d]"
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return $a.Trim().ToLower().StartsWith('y')
}

Write-Host ''
Write-Host '  gerber2laser installer' -ForegroundColor White
Write-Host '  Turns KiCad Gerbers into laser-ready art for CO2 resist etching.'
Write-Host '  This installs Python dependencies only. KiCad is not installed or changed.'

# ---------------------------------------------------------------- 1. Python
Step 1 'Looking for Python 3.9 or newer'
$python = $null
foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
    $exe = Get-Command $cand[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    $argv = @($cand[1..($cand.Count - 1)]) + @('-c', 'import sys; print(sys.version_info[:2] >= (3, 9))')
    $ok = & $exe.Source @argv 2>$null
    if ($ok -eq 'True') { $python = @($exe.Source) + @($cand[1..($cand.Count - 1)]); break }
}
if (-not $python) {
    Write-Host ''
    Write-Host '    No Python 3.9+ found.' -ForegroundColor Red
    Note 'Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),'
    Note 'or from the Microsoft Store, then run this installer again.'
    exit 1
}
$pyVer = & $python[0] @($python[1..($python.Count - 1)] + @('-c', 'import sys; print(sys.version.split()[0])'))
Note "using Python $pyVer  ($($python[0]))"

# --------------------------------------------------------------- 2. Location
Step 2 'Choosing where to install'
if (-not $InstallDir) {
    $InstallDir = Ask 'Install folder' (Join-Path $env:LOCALAPPDATA "Programs\$AppName")
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
Note $InstallDir
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# ----------------------------------------------------------------- 3. Files
Step 3 'Copying application files'
Copy-Item (Join-Path $Source 'gerber2laser.py') $InstallDir -Force
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'assets') | Out-Null
Copy-Item (Join-Path $Source 'assets\*') (Join-Path $InstallDir 'assets') -Force
foreach ($extra in @('README.md', 'requirements.txt', 'LICENSE')) {
    $p = Join-Path $Source $extra
    if (Test-Path $p) { Copy-Item $p $InstallDir -Force }
}
Note 'gerber2laser.py + assets'

# ------------------------------------------------------------------ 4. venv
Step 4 'Creating the virtualenv and installing dependencies'
$venv = Join-Path $InstallDir 'venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
$venvPyw = Join-Path $venv 'Scripts\pythonw.exe'
if (-not (Test-Path $venvPy)) {
    & $python[0] @($python[1..($python.Count - 1)] + @('-m', 'venv', $venv))
}
Note 'installing pyside6 + gerbonara (this pulls ~100 MB and takes a minute)'
& $venvPy -m pip install --upgrade pip --quiet
$req = Join-Path $Source 'requirements.txt'
if (Test-Path $req) { & $venvPy -m pip install -r $req --quiet }
else { & $venvPy -m pip install pyside6 gerbonara --quiet }
& $venvPy -c 'import PySide6, gerbonara' | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host '    dependency check failed' -ForegroundColor Red; exit 1 }
Note 'dependencies OK'

# -------------------------------------------------------------- 5. launcher
Step 5 'Writing the launcher'
$cmd = Join-Path $InstallDir "$AppName.cmd"
@"
@echo off
rem gerber2laser launcher - uses this install's own venv, not system Python
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0gerber2laser.py" %*
"@ | Set-Content -Path $cmd -Encoding ASCII
Note $cmd

# ------------------------------------------------------------- 6. shortcuts
if (-not $NoShortcuts) {
    Step 6 'Creating shortcuts'
    $makeDesktop = AskYN 'Put a shortcut on the Desktop?' $true
    $makeMenu = AskYN 'Add it to the Start Menu?' $true
    $icon = Join-Path $InstallDir 'assets\gerber2laser.ico'
    $shell = New-Object -ComObject WScript.Shell
    function New-Shortcut($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $lnk = $shell.CreateShortcut((Join-Path $dir "$AppName.lnk"))
        $lnk.TargetPath = $venvPyw
        $lnk.Arguments = '"' + (Join-Path $InstallDir 'gerber2laser.py') + '"'
        $lnk.WorkingDirectory = $InstallDir
        $lnk.IconLocation = $icon
        $lnk.Description = 'Gerbers to CO2 laser art (resist etching)'
        $lnk.Save()
        Note (Join-Path $dir "$AppName.lnk")
    }
    if ($makeDesktop) {
        if (-not $DesktopDir) { $DesktopDir = [Environment]::GetFolderPath('Desktop') }
        New-Shortcut $DesktopDir
    }
    if ($makeMenu) {
        if (-not $StartMenuDir) {
            $StartMenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
        }
        New-Shortcut $StartMenuDir
    }
}

# ---------------------------------------------------------- 7. KiCad plugin
if (-not $NoKiCadPlugin) {
    Step 7 'Registering the KiCad plugin (optional)'
    if (-not $KiCadPluginDir) {
        $roots = @()
        foreach ($docs in @([Environment]::GetFolderPath('MyDocuments'),
                            (Join-Path $env:USERPROFILE 'Documents'))) {
            $kc = Join-Path $docs 'KiCad'
            if (Test-Path $kc) {
                $roots += Get-ChildItem $kc -Directory -ErrorAction SilentlyContinue |
                          Sort-Object Name -Descending |
                          ForEach-Object { Join-Path $_.FullName 'scripting\plugins' }
            }
        }
        $KiCadPluginDir = $roots | Select-Object -First 1
    }
    if (-not $KiCadPluginDir) {
        Note 'no KiCad user folder found - skipping (run this again after KiCad has started once)'
    }
    elseif (AskYN "Install the pcbnew plugin into $KiCadPluginDir ?" $true) {
        New-Item -ItemType Directory -Force -Path $KiCadPluginDir | Out-Null
        Copy-Item (Join-Path $InstallDir 'gerber2laser.py') $KiCadPluginDir -Force
        Copy-Item (Join-Path $InstallDir 'assets\gerber2laser_24.png') $KiCadPluginDir -Force
        # KiCad's bundled Python has no PySide6, and a plugin cannot set KiCad's
        # environment, so pin the venv interpreter in a file the plugin reads.
        Set-Content -Path (Join-Path $KiCadPluginDir '.g2l_python') -Value $venvPyw -Encoding ASCII
        Note 'installed - restart pcbnew, then Tools > External Plugins > Gerber to Laser'
    }
}

# -------------------------------------------------------------- 8. uninstall
Step 8 'Writing the uninstaller'
$uninst = Join-Path $InstallDir 'uninstall.ps1'
@"
# Removes gerber2laser. Does not touch KiCad or Python.
`$ErrorActionPreference = 'SilentlyContinue'
Remove-Item -Recurse -Force '$InstallDir'
Remove-Item -Force (Join-Path ([Environment]::GetFolderPath('Desktop')) '$AppName.lnk')
Remove-Item -Force (Join-Path `$env:APPDATA 'Microsoft\Windows\Start Menu\Programs\$AppName.lnk')
Remove-Item -Force '$KiCadPluginDir\gerber2laser.py', '$KiCadPluginDir\.g2l_python', '$KiCadPluginDir\gerber2laser_24.png'
Write-Host 'gerber2laser removed.'
"@ | Set-Content -Path $uninst -Encoding UTF8
Note $uninst

Write-Host ''
Write-Host '  Done.' -ForegroundColor Green
Write-Host "  Start it from the Desktop shortcut, or run: $cmd"
Write-Host ''
