# Stop the cache keep-alive test proxies (ports 18851-18853) and nothing else.
foreach ($port in 18851, 18852, 18853) {
    Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue; "stopped proxy on $port" }
}
