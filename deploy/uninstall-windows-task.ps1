$ErrorActionPreference = "Stop"
Unregister-ScheduledTask -TaskName "Aqua-Quant" -Confirm:$false
Write-Host "Scheduled task removed. Stop any leftover process in Task Manager named pythonw.exe if the page still loads."
