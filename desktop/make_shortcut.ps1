# Create "AgriScout" shortcuts that start the GUI with pythonw.exe (no console window).
#   - on your Desktop
#   - in the repo root (AgriScout.lnk, ignored by git: it holds paths of THIS computer)
# Usage (from anywhere):  powershell -ExecutionPolicy Bypass -File desktop\make_shortcut.ps1
# Run it again if the repo moves. Start AgriScout.bat stays for debugging (shows a console).
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"
$gui = Join-Path $repo "desktop\gui.py"
$icon = Join-Path $repo "desktop\agriscout.ico"
foreach ($path in @($pythonw, $gui, $icon)) {
    if (-not (Test-Path $path)) { throw "missing: $path" }
}

$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath("Desktop")  # also right when OneDrive moved the Desktop
foreach ($folder in @($desktop, $repo)) {
    $lnkPath = Join-Path $folder "AgriScout.lnk"
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = '"' + $gui + '"'
    $lnk.WorkingDirectory = $repo
    $lnk.IconLocation = "$icon,0"
    $lnk.Description = "AgriScout.Zm robot controller"
    $lnk.WindowStyle = 3  # maximized
    $lnk.Save()
    Write-Output "created $lnkPath"
}
Write-Output "Crash log (if anything goes wrong): $env:LOCALAPPDATA\AgriScout\gui.log"
