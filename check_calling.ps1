# Reads .env and prints the phone number's calling settings. Token is never printed.
$envFile = Join-Path $PSScriptRoot ".env"
$cfg = @{}
Get-Content $envFile | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object { $cfg[$Matches[1]] = $Matches[2].Trim() }

if (-not $cfg.WA_TOKEN) { Write-Output "WA_TOKEN is empty in .env - fill it in first."; exit 1 }

$url = "https://graph.facebook.com/$($cfg.WA_GRAPH_VERSION)/$($cfg.WA_PHONE_NUMBER_ID)/settings"
try {
    $res = Invoke-RestMethod -Uri $url -Headers @{ Authorization = "Bearer $($cfg.WA_TOKEN)" }
    $res | ConvertTo-Json -Depth 10
} catch {
    Write-Output "Request failed: $($_.Exception.Message)"
    if ($_.ErrorDetails.Message) { Write-Output $_.ErrorDetails.Message }
    exit 1
}
