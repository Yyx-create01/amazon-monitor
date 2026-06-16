for ($i=1; $i -le 6; $i++) {
    $n = "AmazonMonitor-Batch" + $i
    $t = Get-ScheduledTask -TaskName $n
    $info = Get-ScheduledTaskInfo -TaskName $n
    Write-Host ("{0}: User={1} Logon={2} BatteryBlock={3} Next={4} LastRun={5} Result={6}" -f $n, $t.Principal.UserId, $t.Principal.LogonType, $t.Settings.DisallowStartIfOnBatteries, $info.NextRunTime, $info.LastRunTime, $info.LastTaskResult)
}
