for ($i = 1; $i -le 6; $i++) {
    Unregister-ScheduledTask -TaskName "AmazonMonitor-Batch$i" -Confirm:$false -ErrorAction SilentlyContinue
}

$times = @("08:00", "08:20", "08:40", "09:00", "09:20", "09:40")
for ($i = 0; $i -lt 6; $i++) {
    $n = $i + 1
    $action = New-ScheduledTaskAction -Execute "C:\Users\Administrator\amazon-monitor\run_local.bat" -Argument "--batch $n/6"
    $trigger = New-ScheduledTaskTrigger -Daily -At $times[$i]
    $principal = New-ScheduledTaskPrincipal -UserId "Administrator" -RunLevel Highest
    Register-ScheduledTask -TaskName "AmazonMonitor-Batch$n" -Action $action -Trigger $trigger -Principal $principal -Force
    Write-Host "Batch $n/6 at $($times[$i])"
}
