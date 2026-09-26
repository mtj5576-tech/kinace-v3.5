$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run-kinace-scheduled.ps1"
$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$CurrentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$CurrentSid = $CurrentIdentity.User.Value

if (-not ([System.Security.Principal.WindowsPrincipal]$CurrentIdentity).IsInRole(
    [System.Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    throw "Administrator privileges are required to register S4U tasks."
}

function Grant-BatchLogonRight {
    param([Parameter(Mandatory = $true)][string]$Sid)

    $Config = Join-Path $env:TEMP "kinace-user-rights-$PID.inf"
    $Database = Join-Path $env:TEMP "kinace-user-rights-$PID.sdb"
    try {
        & secedit /export /cfg $Config /areas USER_RIGHTS | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "secedit export failed with exit code $LASTEXITCODE"
        }

        $Lines = [System.IO.File]::ReadAllLines($Config)
        $Index = -1
        for ($i = 0; $i -lt $Lines.Count; $i++) {
            if ($Lines[$i] -match '^\s*SeBatchLogonRight\s*=') {
                $Index = $i
                break
            }
        }

        $Entries = @()
        if ($Index -ge 0) {
            $Entries = @(
                ($Lines[$Index] -split '=', 2)[1].Split(',') |
                    ForEach-Object { $_.Trim() } |
                    Where-Object { $_ }
            )
        }

        $Target = "*$Sid"
        if ($Entries -notcontains $Target) {
            $Entries += $Target
            $NewLine = "SeBatchLogonRight = " + ($Entries -join ',')
            if ($Index -ge 0) {
                $Lines[$Index] = $NewLine
            } else {
                $SectionIndex = -1
                for ($i = 0; $i -lt $Lines.Count; $i++) {
                    if ($Lines[$i] -match '^\s*\[Privilege Rights\]\s*$') {
                        $SectionIndex = $i
                        break
                    }
                }
                if ($SectionIndex -lt 0) {
                    throw "SeBatchLogonRight could not be added: privilege section missing"
                }
                $UpdatedLines = [System.Collections.Generic.List[string]]::new()
                $UpdatedLines.AddRange([string[]]$Lines)
                $UpdatedLines.Insert($SectionIndex + 1, $NewLine)
                $Lines = $UpdatedLines.ToArray()
            }
            [System.IO.File]::WriteAllLines(
                $Config,
                [string[]]$Lines,
                [System.Text.Encoding]::Unicode
            )
        }

        & secedit /configure /db $Database /cfg $Config /areas USER_RIGHTS | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "secedit configure failed with exit code $LASTEXITCODE"
        }
    } finally {
        Remove-Item -LiteralPath $Config, $Database -Force -ErrorAction SilentlyContinue
    }
}

Grant-BatchLogonRight -Sid $CurrentSid

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances Parallel `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

function Register-KinaceTask {
    param(
        [string]$Name,
        [string]$Mode,
        [DayOfWeek[]]$Days,
        [datetime]$At
    )
    $Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -Mode $Mode"
    $Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Days -At $At
    Register-ScheduledTask `
        -TaskName $Name `
        -TaskPath "\Kinace\" `
        -Action $Action `
        -Trigger $Trigger `
        -Principal $Principal `
        -Settings $Settings `
        -Description "Kinace V3.3 $Mode scheduled run" `
        -Force | Out-Null
}

$Weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
foreach ($legacyName in @(
    "Kinace-V32-Preopen",
    "Kinace-V32-Postclose",
    "Kinace-V32-Status",
    "Kinace-V32-Weekly-Research"
)) {
    Get-ScheduledTask -TaskPath "\Kinace\" -TaskName $legacyName -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false
}
Register-KinaceTask -Name "Kinace-V33-Preopen" -Mode "preopen" -Days $Weekdays -At "03:45"
Register-KinaceTask -Name "Kinace-V33-Postclose" -Mode "postclose" -Days $Weekdays -At "11:15"
Register-KinaceTask -Name "Kinace-V33-Status" -Mode "status" -Days $Weekdays -At "11:45"
Register-KinaceTask -Name "Kinace-V33-Weekly-Research" -Mode "weekly" -Days @("Saturday") -At "12:00"

Get-ScheduledTask -TaskPath "\Kinace\" |
    Select-Object TaskName, State, TaskPath
