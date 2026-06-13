$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"C:\Users\noleg\Desktop\Claude\Projects\Stocks\run_probe_alert.bat`""
$trigger = New-ScheduledTaskTrigger -Daily -At "07:00AM"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "ProbeCandidate-Alert" -Action $action -Trigger $trigger -Settings $settings -Description "Daily 7am probe candidate email alert" -Force
Write-Host "SUCCESS: ProbeCandidate-Alert scheduled at 7am daily."
