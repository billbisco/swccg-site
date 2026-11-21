param([Parameter(Mandatory=$true)][string]$Pythonw,[Parameter(Mandatory=$true)][string]$Script)
$ErrorActionPreference='Stop'
$name='SWCCG Operations Dashboard Access'
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw 'Existing task; inspect rather than overwrite' }
if ($Script.Contains('"') -or -not (Test-Path -LiteralPath $Script -PathType Leaf) -or -not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) { throw 'Invalid input' }
$account=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action=New-ScheduledTaskAction -Execute $Pythonw -Argument ('-B "'+$Script+'"')
$trigger=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew -Priority 8 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal=New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Description 'Private dashboard access over existing SSH; loopback only. End and Disable to stop. No console window.' -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
