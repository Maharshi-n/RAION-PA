# One-time setup of Raion on this PC. Safe to run again: it skips what is already done.
# Run in the Raion folder:  powershell -ExecutionPolicy Bypass -File setup.ps1
# Add -Start only on switch day, after Maharshi's laptop has stopped its Raion and ngrok: it starts Raion and adds
# the auto-start task. Without -Start nothing runs by itself.
param([switch]$Start)
$Root = $PSScriptRoot
Set-Location $Root
function Say($text) { Write-Host "== $text" }
function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") }
function Need($name, $id, [scriptblock]$have) {
    if (& $have) { Say "$name ok"; return }
    Say "installing $name"
    winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements
    Refresh-Path
    if (-not (& $have)) { Say "$name still missing: close this window, open a new PowerShell and run setup.ps1 again" }
}

# 1. programs
Need "Git" "Git.Git" { Get-Command git -ErrorAction SilentlyContinue }
function Python312 {  # path of Python 3.12 (py launcher, else the usual install folders), or nothing
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $p = "$(py -3.12 -c "import sys; print(sys.executable)" 2>$null)".Trim()
        if ($p -and (Test-Path $p)) { return $p }
    }
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "C:\Program Files\Python312\python.exe" | Where-Object { Test-Path $_ } | Select-Object -First 1
}
Need "Python 3.12" "Python.Python.3.12" { Python312 }
Need "FFmpeg" "Gyan.FFmpeg" { Get-Command ffmpeg -ErrorAction SilentlyContinue }
Need "LibreOffice" "TheDocumentFoundation.LibreOffice" { Test-Path "C:\Program Files\LibreOffice\program\soffice.com" }
Need "Node.js" "OpenJS.NodeJS.LTS" { Get-Command node -ErrorAction SilentlyContinue }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Say "Claude Code is missing: install it first (claude.ai/install)"; exit 1 }
if ((claude auth status 2>&1 | Out-String) -notmatch '"loggedIn":\s*true') { Say "Claude is not logged in: run 'claude auth login' (paid plan account)" }

# 2. Python packages
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { Say "making .venv"; & (Python312) -m venv .venv }
& $Py -m pip install -q --upgrade pip
& $Py -m pip install -q -r requirements.txt
if ($LASTEXITCODE) { Say "pip install failed: see the errors above"; exit 1 }
Say "Python packages ok"

# 3. this repo pulls with its own read-only key (watchdog.ps1 checks for updates)
$codeKey = "$env:USERPROFILE\.ssh\raion_code_ed25519"
if (Test-Path $codeKey) { git config core.sshCommand "ssh -i '$($codeKey -replace '\\','/')' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" }

# 4. the website code (Raion publishes apps there) with its own write key
$siteKey = "$env:USERPROFILE\.ssh\raion_site_ed25519"
if (-not (Test-Path (Join-Path $Root "site\.git"))) {
    if (-not (Test-Path $siteKey)) { New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null; ssh-keygen -q -t ed25519 -N '""' -C "raion-site-$env:COMPUTERNAME" -f $siteKey }
    $ssh = "ssh -i '$($siteKey -replace '\\','/')' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    $env:GIT_SSH_COMMAND = $ssh
    git clone -q git@github.com:Maharshi-n/raion-custom-dashboards.git site
    Remove-Item Env:GIT_SSH_COMMAND
    if (-not (Test-Path (Join-Path $Root "site\.git"))) {
        Say "Add this key on GitHub: raion-custom-dashboards > Settings > Deploy keys > Add deploy key, TICK 'Allow write access'. Then run setup.ps1 again:"
        Get-Content "$siteKey.pub"
        exit 1
    }
    git -C site config core.sshCommand $ssh
    git -C site config user.name "Raion"
    git -C site config user.email "raion@mndc.maharshinahar.in"
}
Push-Location site; npm install --silent; Pop-Location
Say "website code ok"

# 5. memory and .env come from the pen drive
$ready = (Test-Path (Join-Path $Root "memory\global")) -and (Test-Path (Join-Path $Root ".env"))
if (-not $ready) { Say "memory\ or .env is missing: copy both from the pen drive into $Root" }
else {
    $ok = (& $Py -W ignore check_tools.py 2>&1 | Select-Object -Last 1) -eq "ok"
    Say ("check_tools: " + $(if ($ok) { "ok" } else { "FAILED (run: .venv\Scripts\python.exe check_tools.py)" }))
}

# 6. switch day only (-Start): start now, and by itself at every login, restarted if it ever stops.
#    Not before: a second Raion running beside the laptop's would send every reminder twice.
if (-not $Start) { Say "setup done. On switch day, after the laptop's Raion is stopped: setup.ps1 -Start"; exit 0 }
if (-not $ready) { Say "not starting: memory\ or .env is missing"; exit 1 }
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Root `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Root\watchdog.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
try {
    Register-ScheduledTask -TaskName "Raion" -Action $action -Trigger $trigger -Settings $settings -Force -ErrorAction Stop | Out-Null
} catch { Say "could not add the auto-start task: run PowerShell as Administrator and run setup.ps1 -Start again ($($_.Exception.Message))"; exit 1 }
Start-ScheduledTask -TaskName "Raion"
Say "Raion started, and starts by itself at every login. Log: $Root\logs\watchdog.log"
