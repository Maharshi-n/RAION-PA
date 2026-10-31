# RaionGate: Raion's read-only door to Tally, on the accountant's PC.
# setup.bat runs this with -Install (as admin): it copies itself to Program Files\RaionGate, makes the code Raion
# needs, closes Tally's port 9000 to the network, opens the gate's port to this LAN and starts the gate with Windows
# (hidden). remove.bat runs it with -Remove. With no switch it is the gate: it answers only a fixed list of reads and
# writes every Tally request itself, so nothing that reaches Tally through it can add, change or delete anything.
param([switch]$Install, [switch]$Remove, [int]$Port = 9100, [string]$Listen = '+',
      [string]$Tally = 'http://127.0.0.1:9000/')
$ErrorActionPreference = 'Stop'
$Dir = Join-Path $env:ProgramFiles 'RaionGate'
$Task = 'RaionGate'

function Say($text, $color = 'White') { Write-Host $text -ForegroundColor $color }

if ($Remove) {
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -Group $Task -ErrorAction SilentlyContinue
    Remove-Item $Dir -Recurse -Force -ErrorAction SilentlyContinue
    Say 'RaionGate removed: Raion can no longer read Tally.' Green
    Say '(Tally''s own setting in F1 > Settings > Connectivity is unchanged.)'
    exit
}

if ($Install) {
    New-Item -ItemType Directory -Force $Dir | Out-Null
    $gate = Join-Path $Dir 'gate.ps1'
    if ($PSCommandPath -ne $gate) { Copy-Item $PSCommandPath $gate -Force }
    Unblock-File $gate
    $codeFile = Join-Path $Dir 'code.txt'
    if (-not (Test-Path $codeFile)) {  # kept on a re-install, so the code Raion has stays right
        # 16 of 32 characters = 80 bits: /hello shows a hash of it, so it must be too long to guess even offline
        $abc, $bytes = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789', (New-Object byte[] 16)
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        Set-Content $codeFile (-join ($bytes | ForEach-Object { $abc[$_ % 32] })) -Encoding ASCII
        icacls $codeFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null  # SYSTEM, Administrators
    }
    $code = (Get-Content $codeFile -Raw).Trim()

    Remove-NetFirewallRule -Group $Task -ErrorAction SilentlyContinue
    New-NetFirewallRule -Group $Task -DisplayName 'RaionGate: Tally port 9000 closed to the network' -Direction Inbound `
        -Protocol TCP -LocalPort 9000 -Action Block -Profile Any | Out-Null  # a block rule beats any allow rule
    New-NetFirewallRule -Group $Task -DisplayName 'RaionGate: the gate, for Raion on this LAN' -Direction Inbound `
        -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null
    $off = @(Get-NetFirewallProfile | Where-Object { "$($_.Enabled)" -eq 'False' } | ForEach-Object { $_.Name })

    $run = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$gate`" -Port $Port"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $Task -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $run) `
        -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $settings -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
    Start-ScheduledTask -TaskName $Task

    $up = $false
    foreach ($i in 1..20) {
        Start-Sleep 1
        try { Invoke-WebRequest "http://localhost:$Port/hello" -UseBasicParsing -TimeoutSec 3 | Out-Null; $up = $true; break }
        catch {}
    }
    if (-not $up) { Say 'The gate did not start. Send Maharshi a photo of this window.' Red; exit 1 }
    try {
        $r = Invoke-WebRequest "http://localhost:$Port/read" -Method Post -UseBasicParsing -TimeoutSec 60 `
            -Headers @{ 'X-Gate-Code' = $code } -ContentType 'application/json' -Body '{"read": "companies"}'
        $names = [regex]::Matches($r.Content, '<COMPANY NAME="([^"]*)"') | ForEach-Object { $_.Groups[1].Value }
        Say ('Tally is open: ' + ($names -join ', ')) Green
    } catch {
        Say 'Tally did not answer. That is fine if it is closed: Raion reads it whenever it is open. If it is open,' Yellow
        Say 'check F1 > Settings > Connectivity: TallyPrime acts as Both, port 9000.' Yellow
    }
    Say ''
    Say ('   Done. Code for Raion:   ' + (($code -split '(.{4})' | Where-Object { $_ }) -join '-')) Green
    Say '   Send a photo of this window to Maharshi.' Green
    if ($off) {
        Say ''
        Say ('Note: Windows Firewall is off for ' + ($off -join ', ') + ' networks, so Tally''s port 9000 is still open') Yellow
        Say 'there. Tell Maharshi.' Yellow
    }
    exit
}

# ---------------- the gate ----------------
$Code = (Get-Content (Join-Path $PSScriptRoot 'code.txt') -Raw).Trim()
$sha = [Security.Cryptography.SHA256]::Create()
$Id = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Code))[0..5] | ForEach-Object { $_.ToString('x2') })
$Inv = [Globalization.CultureInfo]::InvariantCulture
$Lists = @{  # read -> Tally object type and the fields Raion gets
    companies    = 'Company', 'Name,StartingFrom,BooksFrom'
    ledgers      = 'Ledger', 'Name,Parent,OpeningBalance,ClosingBalance'
    groups       = 'Group', 'Name,Parent,ClosingBalance'
    vouchertypes = 'VoucherType', 'Name,Parent'
}
$Head = '<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>'
$Format = '<STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'

function Reply($ctx, [int]$status, $body, [string]$type = 'text/plain; charset=utf-8') {
    if ($body -is [string]) { $body = [Text.Encoding]::UTF8.GetBytes($body) }
    $ctx.Response.StatusCode = $status
    $ctx.Response.ContentType = $type
    $ctx.Response.ContentLength64 = $body.Length
    $ctx.Response.OutputStream.Write($body, 0, $body.Length)
    $ctx.Response.Close()
}

function Day([string]$s) {
    if ($s -notmatch '^\d{8}$') { throw 'dates are YYYYMMDD' }
    [datetime]::ParseExact($s, 'yyyyMMdd', $Inv)
}

function Request($q, [switch]$List) {  # the Tally request for one read, made only from fixed parts and checked values
    # $List: entries as a list of vouchers filtered by date, instead of the Day Book
    $vars = ''
    if ($q.company) {
        if ("$($q.company)".Length -gt 200) { throw 'company name too long' }
        $vars += '<SVCURRENTCOMPANY>' + [Security.SecurityElement]::Escape("$($q.company)") + '</SVCURRENTCOMPANY>'
    }
    $from = $null; $to = $null
    if ($q.from) { $from = Day $q.from }
    if ($q.to) { $to = Day $q.to }
    if ($from -and -not $to) { throw 'from needs to' }
    if ($to) {
        if (-not $from) { $from = $to }
        if ($to -lt $from) { throw 'to is before from' }
        $f, $t = $from.ToString('d-MMM-yyyy', $Inv), $to.ToString('d-MMM-yyyy', $Inv)  # Tally's own way: 1-Aug-2026
        $vars += '<SVFROMDATE TYPE="Date">' + $f + '</SVFROMDATE><SVTODATE TYPE="Date">' + $t + '</SVTODATE>'
    }
    $read = "$($q.read)"
    if ($read -eq 'vouchers') {
        if (-not $to) { throw 'vouchers need from and to' }
        if (($to - $from).Days -gt 30) { throw 'vouchers: at most 31 days per read' }
        if (-not $List) {
            return $Head + '<TYPE>Data</TYPE><ID>DayBook</ID></HEADER><BODY><DESC>' + $Format + $vars + '</STATICVARIABLES></DESC></BODY></ENVELOPE>'
        }
        return $Head + '<TYPE>Collection</TYPE><ID>RaionList</ID></HEADER><BODY><DESC>' + $Format + $vars + '</STATICVARIABLES><TDL><TDLMESSAGE>' +
            '<COLLECTION NAME="RaionList" ISINITIALIZE="Yes"><TYPE>Voucher</TYPE><FETCH>Date,VoucherTypeName,VoucherNumber,' +
            'PartyLedgerName,Narration,IsCancelled,IsOptional,AllLedgerEntries.*</FETCH><FILTER>RaionDays</FILTER></COLLECTION>' +
            '<SYSTEM TYPE="Formulae" NAME="RaionDays">$Date &gt;= $$Date:"' + $f + '" AND $Date &lt;= $$Date:"' + $t + '"</SYSTEM>' +
            '</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>'
    }
    if (-not $Lists.ContainsKey($read)) { throw "unknown read '$read'" }
    $type, $fields = $Lists[$read]
    $m = -join ($fields -split ',' | ForEach-Object { "<NATIVEMETHOD>$_</NATIVEMETHOD>" })
    $Head + '<TYPE>Collection</TYPE><ID>RaionList</ID></HEADER><BODY><DESC>' + $Format + $vars + '</STATICVARIABLES><TDL><TDLMESSAGE>' +
        '<COLLECTION NAME="RaionList" ISINITIALIZE="Yes"><TYPE>' + $type + '</TYPE>' + $m + '</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>'
}

function Ask-Tally([string]$xml) {
    $req = [Net.HttpWebRequest]::Create($Tally)
    $req.Method = 'POST'
    $req.ContentType = 'text/xml; charset=utf-8'
    $req.Proxy = $null
    $req.Timeout = 120000
    $req.ReadWriteTimeout = 120000
    $b = [Text.Encoding]::UTF8.GetBytes($xml)
    $s = $req.GetRequestStream()
    $s.Write($b, 0, $b.Length)
    $s.Close()
    $resp = $req.GetResponse()
    try {
        $ms = New-Object IO.MemoryStream
        $resp.GetResponseStream().CopyTo($ms)
        @{ body = $ms.ToArray(); type = $resp.ContentType }
    } finally { $resp.Close() }
}

function Right-Days($body, $q) {  # Tally's answer has entries, all dated within the asked days
    if ($body.Length -gt 3 -and ($body[1] -eq 0 -or $body[3] -eq 0)) { $text = [Text.Encoding]::Unicode.GetString($body) }
    else { $text = [Text.Encoding]::UTF8.GetString($body) }
    $days = @([regex]::Matches($text, '<VOUCHER\b[\s\S]*?<DATE\b[^>]*>(\d{8})</DATE>') | ForEach-Object { $_.Groups[1].Value })
    $from = "$($q.from)"
    if (-not $from) { $from = "$($q.to)" }
    $days.Count -gt 0 -and -not ($days | Where-Object { $_ -lt $from -or $_ -gt "$($q.to)" })
}

try {
    $l = New-Object Net.HttpListener
    $l.Prefixes.Add("http://${Listen}:$Port/")
    $l.Start()
} catch {
    Add-Content (Join-Path $PSScriptRoot 'gate.log') "$(Get-Date -Format s) could not start: $_"
    exit 1  # the task tries again in a minute
}
while ($true) {
    $ctx = $l.GetContext()  # one at a time: Tally answers one by one anyway
    try {
        $path = $ctx.Request.Url.AbsolutePath
        if ($path -eq '/hello') { Reply $ctx 200 "RaionGate $Id"; continue }
        if ($path -ne '/read' -or $ctx.Request.HttpMethod -ne 'POST') { Reply $ctx 404 'not here'; continue }
        $given = ("$($ctx.Request.Headers['X-Gate-Code'])" -replace '[^A-Za-z0-9]', '').ToUpper()
        if ($given -cne $Code) { Start-Sleep -Milliseconds 500; Reply $ctx 403 'wrong code'; continue }
        if ($ctx.Request.ContentLength64 -gt 4096) { Reply $ctx 413 'too big'; continue }
        $q = (New-Object IO.StreamReader($ctx.Request.InputStream, [Text.Encoding]::UTF8)).ReadToEnd() | ConvertFrom-Json
        try { $xml = Request $q } catch { Reply $ctx 400 "$($_.Exception.Message)"; continue }
        try {
            $t = Ask-Tally $xml
            if ("$($q.read)" -eq 'vouchers' -and -not (Right-Days $t.body $q)) {
                # the Day Book can come back for Tally's own date, whatever dates it was given: ask for the same days
                # as a list of vouchers and keep that answer if it has them (Raion leaves out entries of other days)
                try { $u = Ask-Tally (Request $q -List); if (Right-Days $u.body $q) { $t = $u } } catch {}
            }
        }
        catch {
            $w = $_.Exception.InnerException
            if ($w -is [Net.WebException] -and "$($w.Status)" -eq 'Timeout') {
                Reply $ctx 504 'Tally took too long to answer (busy?): try a shorter date range'
            } else {
                Reply $ctx 503 'Tally is not open, or its F1 > Settings > Connectivity is not set to Both, port 9000'
            }
            continue
        }
        $type = 'text/xml; charset=utf-8'
        if ($t.type) { $type = $t.type }
        Reply $ctx 200 $t.body $type
    } catch {
        try { Reply $ctx 500 "$($_.Exception.Message)" } catch {}
    }
}
