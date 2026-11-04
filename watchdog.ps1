# Keeps Raion running on this PC: the server, the ngrok tunnel, and code updates from GitHub (every 10 minutes).
# Started hidden at logon by the "Raion" task (setup.ps1 -Start). Log: logs\watchdog.log
$Root = $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Logs = Join-Path $Root "logs"
$Busy = Join-Path $Root ".runtime\busy"
$Asked = Join-Path $Root "UPDATE_NOW"  # Raion makes this file after an admin says yes to an update: check now, not in 10 minutes
New-Item -ItemType Directory -Force $Logs | Out-Null
$Port = 8080
$m = Select-String -Path (Join-Path $Root ".env") -Pattern '^\s*PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue
if ($m) { $Port = [int]@($m)[0].Matches[0].Groups[1].Value }
$Ngrok = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
if (-not $Ngrok) { $Ngrok = Get-ChildItem "D:\ngrok", "C:\ngrok", "$env:USERPROFILE\ngrok" -Recurse -Filter ngrok.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName }

function Log($text) { Add-Content (Join-Path $Logs "watchdog.log") ("{0:dd/MM HH:mm:ss}  {1}" -f (Get-Date), $text) }

function Stop-Tree($proc) { if ($proc -and -not $proc.HasExited) { taskkill /PID $proc.Id /T /F 2>&1 | Out-Null } }

function Start-Server {
    foreach ($n in "server.log", "server.err.log") {  # keep the previous run's log as .old
        $f = Join-Path $Logs $n
        if (Test-Path $f) { Move-Item $f "$f.old" -Force -ErrorAction SilentlyContinue }
    }
    Remove-Item $Busy -ErrorAction SilentlyContinue  # a fresh server has nobody mid-reply
    $p = Start-Process $Py -ArgumentList "-W", "ignore", "-u", "app.py" -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "server.log") -RedirectStandardError (Join-Path $Logs "server.err.log")
    $null = $p.Handle  # keeps the exit code readable later (PowerShell 5.1)
    $p
}

function Quiet {  # no chat mid-reply or call on, and none in the last 3 minutes (Raion writes .runtime\busy)
    $f = Get-Item $Busy -ErrorAction SilentlyContinue
    if (-not $f) { return $true }
    $age = ((Get-Date) - $f.LastWriteTime).TotalMinutes
    ($age -ge 60) -or ("$(Get-Content $f.FullName -Raw)".Trim() -eq "0" -and $age -ge 3)  # 60: a stuck mark never blocks updates
}

function Check-Tools { (& $Py -W ignore (Join-Path $Root "check_tools.py") 2>&1 | Select-Object -Last 1) -eq "ok" }

function Install-Packages { & $Py -m pip install -q -r (Join-Path $Root "requirements.txt") 2>&1 | Out-Null }

# a fresh start: nothing left over from an earlier watchdog
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*app.py*" -and $_.ExecutablePath -like "$Root*" } |
    ForEach-Object { taskkill /PID $_.ProcessId /T /F 2>&1 | Out-Null }
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Log "watchdog started (port $Port, ngrok: $Ngrok)"
if (-not $Ngrok) { Log "ngrok.exe not found: WhatsApp can't reach Raion" }

$server = $null; $tunnel = $null; $serverAt = [datetime]::MinValue; $tunnelAt = [datetime]::MinValue
$strikes = 0; $lastCheck = Get-Date; $lastUpdate = Get-Date; $badCommit = ""
while ($true) {
    # ngrok and the server: started again if they stop, at most once a minute (a crash loop doesn't spin)
    if ($Ngrok -and (-not $tunnel -or $tunnel.HasExited) -and ((Get-Date) - $tunnelAt).TotalSeconds -ge 60) {
        if ($tunnel) { Log "ngrok stopped, starting again" }
        $tunnel = Start-Process $Ngrok -ArgumentList "http", "127.0.0.1:$Port" -WindowStyle Hidden -PassThru
        $tunnelAt = Get-Date; Log "ngrok started"
    }
    if (-not $server -or $server.HasExited) {
        if (((Get-Date) - $serverAt).TotalSeconds -ge 60) {
            if ($server) { Log "server stopped (exit $($server.ExitCode)), starting again" }
            $server = Start-Server; $serverAt = Get-Date; $strikes = 0; $lastCheck = Get-Date; Log "server started"
        }
    }
    elseif (((Get-Date) - $lastCheck).TotalSeconds -ge 60) {  # running but not answering 3 minutes in a row: restart
        $lastCheck = Get-Date
        try { Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 10 | Out-Null; $strikes = 0 }
        catch { $strikes++; if ($strikes -ge 3) { Log "server not answering, restarting"; Stop-Tree $server } }
    }
    # code updates from GitHub, only while nobody is mid-chat: every 10 minutes, or every 30 seconds while an admin's ask waits
    $asked = Test-Path $Asked
    $since = ((Get-Date) - $lastUpdate).TotalSeconds
    if ($since -ge 600 -or ($asked -and $since -ge 30)) {
        $lastUpdate = Get-Date
        git -C $Root fetch -q origin main 2>&1 | Out-Null
        $old = git -C $Root rev-parse HEAD
        $new = git -C $Root rev-parse origin/main
        if ($asked -and $new -and $new -eq $old) {
            Remove-Item $Asked -Force -ErrorAction SilentlyContinue; Log "update asked for: already on the newest code"
        }
        elseif ($asked -and $new -and $new -eq $badCommit) {
            Remove-Item $Asked -Force -ErrorAction SilentlyContinue
            Log "update asked for: $($new.Substring(0,7)) failed check_tools before: kept the old code"
        }
        if ($new -and $old -and $new -ne $old -and $new -ne $badCommit -and (Quiet)) {
            if ($asked) { Remove-Item $Asked -Force -ErrorAction SilentlyContinue; Log "update asked for by an admin" }
            Log "updating $($old.Substring(0,7)) -> $($new.Substring(0,7))"
            Stop-Tree $server; $server = $null; $serverAt = [datetime]::MinValue  # restarted below on whichever code stays
            $packages = git -C $Root diff --name-only $old $new -- requirements.txt
            git -C $Root reset -q --hard $new
            if ($packages) { Install-Packages }
            if (Check-Tools) { Log "update ok" }
            else {
                git -C $Root reset -q --hard $old
                if ($packages) { Install-Packages }
                $badCommit = $new; Log "update $($new.Substring(0,7)) FAILED check_tools: kept the old code"
            }
        }
    }
    Start-Sleep 15
}
