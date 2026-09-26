$ErrorActionPreference = "Stop"

Get-ScheduledTask -TaskPath "\Kinace\" -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false
