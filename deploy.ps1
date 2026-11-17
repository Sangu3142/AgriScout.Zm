# Copy robot/*.py to the Pi and syntax-check them with the Pi's Python 3.7.3.
# Usage: .\deploy.ps1
$ErrorActionPreference = "Stop"
$robotDir = Join-Path $PSScriptRoot "robot"

ssh agriscout "mkdir -p ~/agriscout/robot/tools"
if ($LASTEXITCODE -ne 0) { throw "ssh mkdir failed" }

# PowerShell doesn't expand wildcards for scp, so list the files ourselves (skips __pycache__).
$files = Get-ChildItem -Path $robotDir -Filter *.py | ForEach-Object { $_.FullName }
scp $files "agriscout:~/agriscout/robot/"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

# Hardware test tools (robot/tools, e.g. dht_test.py). They import robot/*.py from the folder above.
$toolsDir = Join-Path $robotDir "tools"
$tools = Get-ChildItem -Path $toolsDir -Filter *.py | ForEach-Object { $_.FullName }
scp $tools "agriscout:~/agriscout/robot/tools/"
if ($LASTEXITCODE -ne 0) { throw "scp tools failed" }

# systemd unit file: only COPIED next to the code. Installing it is a separate, manual step
# (see CLAUDE.md "Start at boot"), deploy never touches /etc/systemd.
scp (Join-Path $robotDir "agriscout.service") "agriscout:~/agriscout/robot/"
if ($LASTEXITCODE -ne 0) { throw "scp agriscout.service failed" }

# Phone dashboard (robot/web: html, js, css, manifest, icons; a few KB).
scp -r (Join-Path $robotDir "web") "agriscout:~/agriscout/robot/"
if ($LASTEXITCODE -ne 0) { throw "scp web failed" }

ssh agriscout "cd ~/agriscout/robot && python3 -m py_compile *.py tools/*.py && echo 'py_compile OK (3.7)'"
if ($LASTEXITCODE -ne 0) { throw "py_compile failed on the Pi" }

Write-Output "Deployed $($files.Count + $tools.Count) files to agriscout:~/agriscout/robot"
