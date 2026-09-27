# 윈도우 작업 스케줄러에 30분마다 실행 등록 (창 없이 실행)
# 실행:  powershell -ExecutionPolicy Bypass -File register_task.ps1 [-Minutes 30]
param([int]$Minutes = 30, [string]$Name = "chzzk2yt")
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { throw "먼저 setup.ps1 을 실행하세요 ($pyw 없음)" }

$action  = New-ScheduledTaskAction -Execute $pyw -Argument "-m chzzk2yt run" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
           -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 0)   # 0 = 제한 없음 (긴 VOD)
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings `
    -Description "치지직 다시보기 → 유튜브 비공개 업로드" -Force | Out-Null
Write-Host "등록 완료: '$Name' ($Minutes 분마다). 해제: Unregister-ScheduledTask -TaskName $Name"
Write-Host "로그: $root\data\logs\chzzk2yt.log   상태표: $root\data\status.html"
