$action = New-ScheduledTaskAction -Execute "C:\Users\Administrator\amazon-monitor\run_local.bat"
$trigger = New-ScheduledTaskTrigger -Daily -At 09:00
$principal = New-ScheduledTaskPrincipal -UserId "Administrator" -RunLevel Highest
Register-ScheduledTask -TaskName "AmazonListingMonitor" -Action $action -Trigger $trigger -Principal $principal -Force
Write-Host "Task created successfully. Daily run at 09:00."
