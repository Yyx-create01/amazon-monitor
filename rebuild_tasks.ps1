# Delete old tasks
for ($i=1; $i -le 6; $i++) {
    schtasks /delete /tn "AmazonMonitor-Batch$i" /f 2>$null
}

$base = "C:\Users\Administrator\Desktop\项目文件\自己ASIN监控"
$times = @("08:00", "08:20", "08:40", "09:00", "09:20", "09:40")
for ($i = 0; $i -lt 6; $i++) {
    $n = "AmazonMonitor-Batch" + ($i + 1)
    $batch = ($i + 1).ToString() + "/6"
    $cmd = "`"$base\run_local.bat`" --batch $batch"
    schtasks /create /tn $n /tr $cmd /sc daily /st $times[$i] /ru Administrator /rl highest /f
    Write-Host ("$n at $($times[$i]): created")
}
