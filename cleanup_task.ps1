Unregister-ScheduledTask -TaskName "AmazonMonitorTrigger" -Confirm:$false -ErrorAction SilentlyContinue
Get-ScheduledTask -TaskName "AmazonMonitor*" | Format-Table TaskName, State
