# Registers Aqua-Quant as a hidden logon task. No extra software.
# From the unzipped aqua-quant folder:
#   powershell -ExecutionPolicy Bypass -File .\deploy\install-windows-task.ps1
$ErrorActionPreference = "Stop"
$AppDir = Split-Path $PSScriptRoot -Parent
$Server = Join-Path $AppDir "server.py"
if (-not (Test-Path $Server)) { throw "server.py not found. Run this from the unzipped aqua-quant\deploy folder." }

$Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $Python) { throw "Python is not on PATH. Install Python 3.10+ and enable 'Add python.exe to PATH'." }

$Wrapper = Join-Path $AppDir "start-service.cmd"
@"
@echo off
set AQUA_HOST=127.0.0.1
set AQUA_PORT=8765
cd /d "$AppDir"
"$Python" "$Server" >> "$AppDir\aqua-quant.log" 2>&1
"@ | Set-Content -Encoding ASCII $Wrapper

$TaskName = "Aqua-Quant"
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Wrapper`"" -WorkingDirectory $AppDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Aqua-Quant paper terminal on 127.0.0.1:8765" | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started. Open http://127.0.0.1:8765"
Write-Host "Log: $AppDir\aqua-quant.log"
