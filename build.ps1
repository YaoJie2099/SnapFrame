# Build SnapFrame.exe and SnapFrame-Setup-<version>.exe  (run from this folder, PowerShell 7).
# Needs: Python 3 with pillow, pystray, pyinstaller   ->   py -m pip install --user pillow pystray pyinstaller
# Intermediate files go to %TEMP% (OneDrive locks files inside build\ and breaks PyInstaller's cleanup);
# only the finished installer is copied to .\dist\
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
$ver = & $py -c "from branding import VERSION; print(VERSION)"
$work = Join-Path $env:TEMP 'snapframe-build'
$ico = Join-Path $PSScriptRoot 'assets\snapframe.ico'
$work = Join-Path $work ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $work, assets, dist | Out-Null


# 1) the app
& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name SnapFrame --icon $ico `
    --add-data "${ico};assets" --hidden-import pystray._win32 --workpath "$work\w1" --distpath "$work\app" --specpath "$work\spec" snapframe.py
if ($LASTEXITCODE) { throw 'app build failed' }

# 2) the installer, with the app embedded
& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name "SnapFrame-Setup-$ver" --icon $ico `
    --add-data "$work\app\SnapFrame.exe;." --add-data "${ico};." `
    --workpath "$work\w2" --distpath "$work\setup" --specpath "$work\spec" installer.py
if ($LASTEXITCODE) { throw 'installer build failed' }

Copy-Item "$work\setup\SnapFrame-Setup-$ver.exe" dist\ -Force
Get-FileHash "dist\SnapFrame-Setup-$ver.exe" -Algorithm SHA256 | Format-List Hash, Path