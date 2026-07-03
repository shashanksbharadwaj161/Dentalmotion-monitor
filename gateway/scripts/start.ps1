# Start the Gateway on the host (Windows).
# Running on the host preserves the real device UDP source IP,
# which is required for the gateway to send UDP commands back to devices.
#
# Usage: .\start.ps1
param()
$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir     = Resolve-Path (Join-Path $ScriptDir "..")
$RunDir     = Join-Path $AppDir ".run"
$PidFile    = Join-Path $RunDir "gateway.pid"
$LogFile    = Join-Path $RunDir "gateway.log"
$ErrFile    = Join-Path $RunDir "gateway.err"
$ConfigFile = Join-Path $RunDir "config.json"
$VenvDir    = Join-Path $AppDir ".venv"
$PythonBin  = Join-Path $VenvDir "Scripts\python.exe"
$PipBin     = Join-Path $VenvDir "Scripts\pip.exe"

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

# Stop any existing instance
if (Test-Path $PidFile) {
    $OldPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($OldPid) {
        try {
            Stop-Process -Id ([int]$OldPid) -Force -ErrorAction Stop
            Write-Host "Stopped previous instance (PID $OldPid)."
        } catch {}
    }
    Remove-Item $PidFile -Force
}

function Assert-PortFree {
    param(
        [ValidateSet("UDP", "TCP")]
        [string]$Protocol,
        [int]$Port
    )

    if ($Protocol -eq "UDP") {
        $Endpoints = @(Get-NetUDPEndpoint -LocalPort $Port -ErrorAction SilentlyContinue)
    } else {
        $Endpoints = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    }
    if ($Endpoints.Count -eq 0) {
        return
    }

    $Names = @(
        $Endpoints |
            ForEach-Object { Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue } |
            Select-Object -ExpandProperty ProcessName -Unique
    )
    if (($Names -join " ") -match "docker") {
        throw "Legacy Docker Gateway is still using $Protocol/$Port. Stop and remove the old newhorizons-gateway container before starting the host-only Gateway."
    }
    throw "$Protocol/$Port is already in use by: $($Names -join ', ')"
}

Assert-PortFree -Protocol UDP -Port 22346
Assert-PortFree -Protocol UDP -Port 13250
Assert-PortFree -Protocol TCP -Port 5052

# Create virtualenv on first run
if (-not (Test-Path $PythonBin)) {
    Write-Host "Creating .venv ..."
    python -m venv $VenvDir
}

& $PipBin install -q -r (Join-Path $AppDir "requirements.txt")
& $PipBin install -q -e $AppDir

# Create config from example on first run
if (-not (Test-Path $ConfigFile)) {
    Copy-Item (Join-Path $AppDir "config.example.json") $ConfigFile
    Write-Host "Created $ConfigFile - open the WebUI to finish setup."
}

# Launch
Set-Content -Path $LogFile -Value ""
Set-Content -Path $ErrFile -Value ""

$env:PYTHONUNBUFFERED = "1"
$env:NEWHORIZONS_GATEWAY_APP_ROOT = $AppDir
$env:NEWHORIZONS_GATEWAY_RESTART_COMMAND = "powershell -File `"$ScriptDir\start.ps1`""
$Process = Start-Process -FilePath $PythonBin `
    -ArgumentList @("-m", "newhorizons_gateway.main", "--config", $ConfigFile) `
    -WorkingDirectory $AppDir `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError $ErrFile `
    -PassThru -WindowStyle Hidden

Set-Content -Path $PidFile -Value $Process.Id

Start-Sleep -Seconds 1
$Process.Refresh()
if ($Process.HasExited) {
    Write-Error "Gateway failed to start."
    Write-Host "--- stdout ---"
    if (Test-Path $LogFile) { Get-Content $LogFile -TotalCount 30 }
    Write-Host "--- stderr ---"
    if (Test-Path $ErrFile) { Get-Content $ErrFile -TotalCount 30 }
    exit 1
}

Write-Host "Gateway started   PID $($Process.Id)"
Write-Host "WebUI             http://127.0.0.1:5052"
Write-Host "Log               $LogFile"
