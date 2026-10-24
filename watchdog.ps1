# Keeps Raion running on this PC: the server, the ngrok tunnel, and code updates from GitHub (every 10 minutes).
# Started hidden at logon by the "Raion" task (setup.ps1). Log: logs\watchdog.log
$Root = $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Force $Logs | Out-Null
$Port = 8080
$m = Select-String -Path (Join-Path $Root ".env") -Pattern '^\s*PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue
if ($m) { $Port = [int]$m.Matches[0].Groups[1].Value }
$Ngrok = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
if (-not $Ngrok) { $Ngrok = Get-ChildItem "D:\ngrok", "C:\ngrok", "$env:USERPROFILE\ngrok" -Recurse -Filter ngrok.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName }

function Log($text) { Add-Content (Join-Path $Logs "watchdog.log") ("{0:dd/MM HH:mm:ss}  {1}" -f (Get-Date), $text) }

function Stop-Tree($proc) { if ($proc -and -not $proc.HasExited) { taskkill /PID $proc.Id /T /F 2>&1 | Out-Null } }

function Start-Server {
    foreach ($n in "server.log", "server.err.log") {  # keep the previous run's log as .old
        $f = Join-Path $Logs $n
        if (Test-Path $f) { Move-Item $f "$f.old" -Force -ErrorAction SilentlyContinue }
    }
    $p = Start-Process $Py -ArgumentList "-W", "ignore", "-u", "app.py" -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "server.log") -RedirectStandardError (Join-Path $Logs "server.err.log")
    $null = $p.Handle  # keeps the exit code readable later (PowerShell 5.1)
    $p
}

function Quiet {  # no chat mid-reply, and none in the last 3 minutes (Raion writes .runtime\busy)
    $f = Get-Item (Join-Path $Root ".runtime\busy") -ErrorAction SilentlyContinue
    -not $f -or ((Get-Content $f.FullName -Raw).Trim() -eq "0" -and ((Get-Date) - $f.LastWriteTime).TotalMinutes -ge 3)
}

function Check-Tools { (& $Py -W ignore (Join-Path $Root "check_tools.py") 2>&1 | Select-Object -Last 1) -eq "ok" }

# a fresh start: nothing left over from an earlier watchdog
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*app.py*" -and $_.ExecutablePath -like "$Root*" } |
    ForEach-Object { taskkill /PID $_.ProcessId /T /F 2>&1 | Out-Null }
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Log "watchdog started (port $Port, ngrok: $Ngrok)"

$server = $null; $tunnel = $null; $strikes = 0; $lastCheck = Get-Date; $lastUpdate = Get-Date; $badCommit = ""
while ($true) {
    if (-not $tunnel -or $tunnel.HasExited) {
        if ($Ngrok) { $tunnel = Start-Process $Ngrok -ArgumentList "http", $Port -WindowStyle Hidden -PassThru; Log "ngrok started" }
        else { Log "ngrok.exe not found"; Start-Sleep 300 }
    }
    if (-not $server -or $server.HasExited) {
        if ($server) { Log "server stopped (exit $($server.ExitCode)), starting again" }
        $server = Start-Server; $strikes = 0; $lastCheck = Get-Date; Log "server started"
    }
    elseif (((Get-Date) - $lastCheck).TotalSeconds -ge 60) {  # running but not answering 3 minutes in a row: restart
        $lastCheck = Get-Date
        try { Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 10 | Out-Null; $strikes = 0 }
        catch { $strikes++; if ($strikes -ge 3) { Log "server not answering, restarting"; Stop-Tree $server; $server = $null } }
    }
    if (((Get-Date) - $lastUpdate).TotalMinutes -ge 10) {  # code updates from GitHub
        $lastUpdate = Get-Date
        git -C $Root fetch -q origin main 2>&1 | Out-Null
        $old = git -C $Root rev-parse HEAD
        $new = git -C $Root rev-parse origin/main
        if ($new -and $new -ne $old -and $new -ne $badCommit -and (Quiet)) {
            Log "updating $($old.Substring(0,7)) -> $($new.Substring(0,7))"
            git -C $Root reset -q --hard $new
            & $Py -m pip install -q -r (Join-Path $Root "requirements.txt") 2>&1 | Out-Null
            if (Check-Tools) {
                Stop-Tree $server; $server = $null; Log "update ok, server restarting"
            } else {
                git -C $Root reset -q --hard $old
                & $Py -m pip install -q -r (Join-Path $Root "requirements.txt") 2>&1 | Out-Null
                $badCommit = $new; Log "update $($new.Substring(0,7)) FAILED check_tools: kept the old code"
            }
        }
    }
    Start-Sleep 15
}
