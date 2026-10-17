# Windows: サインイン時に VOICEVOX エンジンと Bot を自動起動します
# 使い方: PowerShell で  .\install_autostart.ps1 -EnginePath "C:\voicevox_engine\windows-cpu\run.exe"
param(
    [string]$EnginePath = "C:\voicevox_engine\windows-cpu\run.exe"
)

if (-not (Test-Path $EnginePath)) {
    Write-Error "エンジンが見つかりません: $EnginePath"
    exit 1
}

$botDir = $PSScriptRoot
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "VoicevoxEngine" -Force -Settings $settings `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
    -Action (New-ScheduledTaskAction -Execute $EnginePath -WorkingDirectory (Split-Path $EnginePath))

$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT30S"   # エンジンの準備待ち
Register-ScheduledTask -TaskName "ZundaBot" -Force -Settings $settings -Trigger $trigger `
    -Action (New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$botDir\start.bat`"" -WorkingDirectory $botDir)

# スリープで止まらないようにする(電源接続時)
powercfg /change standby-timeout-ac 0

Write-Host "登録しました。PCを再起動してサインインすると、自動で起動します。"
