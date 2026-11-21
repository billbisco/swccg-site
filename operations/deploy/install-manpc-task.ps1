param(
    [Parameter(Mandatory=$true)][string]$Pythonw,
    [Parameter(Mandatory=$true)][string]$Script,
    [Parameter(Mandatory=$true)][string]$WikiOps,
    [Parameter(Mandatory=$true)][string]$StateDir
)
$ErrorActionPreference = 'Stop'
$name = 'SWCCG MANPC Verification'
foreach ($p in @($Pythonw,$Script,$WikiOps,$StateDir)) {
    if ($p.Contains('"') -or -not [IO.Path]::IsPathRooted($p)) { throw 'Expected absolute path without quotes' }
}
if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf) -or -not (Test-Path -LiteralPath $Script -PathType Leaf) -or -not (Test-Path -LiteralPath $WikiOps -PathType Container)) { throw 'Missing task input' }
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw 'Task already exists; inspect before changing it' }
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$argument = '-B "' + $Script + '" --wiki-ops "' + $WikiOps + '" --state-dir "' + $StateDir + '"'
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument $argument -WorkingDirectory $StateDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew -Priority 8 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
Register-ScheduledTask -TaskName $name -Description 'SWCCG existing capture verification only; three candidates per half hour; no save submissions. Disable or stop here at any time.' -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
Get-ScheduledTask -TaskName $name | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $name | Select-Object LastRunTime,LastTaskResult,NextRunTime
