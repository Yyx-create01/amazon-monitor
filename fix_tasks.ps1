# Remove old tasks
for ($i=1; $i -le 6; $i++) {
    Unregister-ScheduledTask -TaskName "AmazonMonitor-Batch$i" -Confirm:$false -ErrorAction SilentlyContinue
}

$times = @("08:00", "08:20", "08:40", "09:00", "09:20", "09:40")
for ($i = 0; $i -lt 6; $i++) {
    $n = "AmazonMonitor-Batch" + ($i + 1)
    $action = New-ScheduledTaskAction -Execute "C:\Users\Administrator\amazon-monitor\run_local.bat" -Argument "--batch $($i+1)/6"
    $trigger = New-ScheduledTaskTrigger -Daily -At $times[$i]
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $n -Action $action -Trigger $trigger -Principal $principal -Force

    # Fix settings after creation
    $t = Get-ScheduledTask -TaskName $n
    $t.Settings.DisallowStartIfOnBatteries = $false
    $t.Settings.StopIfGoingOnBatteries = $false
    $t.Settings.StartWhenAvailable = $true
    $t.Settings.AllowStartIfOnBatteries = $true
    Set-ScheduledTask -TaskName $n -Settings $t.Settings

    Write-Host ("{0} at {1}: SYSTEM, battery=off" -f $n, $times[$i])
}
