param(
    [string]$Sender = "gmailsecondarybackup@gmail.com"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "Enter a Gmail app password for $Sender."
$securePassword = Read-Host "Gmail app password" -AsSecureString
$passwordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPtr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPtr)
}

$env:MAMS_EMAIL_BACKEND = "smtp"
$env:MAMS_SMTP_HOST = "smtp.gmail.com"
$env:MAMS_SMTP_PORT = "587"
$env:MAMS_SMTP_USERNAME = $Sender
$env:MAMS_SMTP_PASSWORD = $password
$env:MAMS_SMTP_FROM = $Sender
$env:MAMS_DB = Join-Path $root "instance\mams-local-test.db"
$password = $null

try {
@'
import os
import smtplib
import ssl

with smtplib.SMTP(os.environ["MAMS_SMTP_HOST"], int(os.environ["MAMS_SMTP_PORT"]), timeout=20) as server:
    server.ehlo()
    server.starttls(context=ssl.create_default_context())
    server.ehlo()
    server.login(os.environ["MAMS_SMTP_USERNAME"], os.environ["MAMS_SMTP_PASSWORD"])
print("SMTP authentication succeeded. No email was sent.")
'@ | & ".\.venv\Scripts\python.exe" -
if ($LASTEXITCODE -ne 0) {
    throw "SMTP authentication failed. Use a valid Gmail app password; no email was sent."
}

    $portOwner = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($listenerPid in $portOwner) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerPid"
        if ($process.Name -ne "python.exe" -or
            $process.CommandLine -notlike "*$root*python.exe* -m flask --app app run*") {
            throw "Port 8000 is occupied by a different process (PID $listenerPid). Stop it manually, then rerun this script."
        }
        Stop-Process -Id $listenerPid -Force
    }

    $serverOutput = Join-Path $env:TEMP "mams-local-test-server.out.log"
    $serverErrors = Join-Path $env:TEMP "mams-local-test-server.err.log"
    $server = Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") `
        -ArgumentList @("-m", "flask", "--app", "app", "run", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $serverOutput `
        -RedirectStandardError $serverErrors -PassThru

    $ready = $false
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        Start-Sleep -Seconds 1
        if ($server.HasExited) {
            break
        }
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:8000/login" -UseBasicParsing -TimeoutSec 2
            $ready = $true
            break
        } catch {
            # The Flask server may need a moment to initialize.
        }
    }
    if (-not $ready) {
        if (Test-Path $serverErrors) {
            Get-Content $serverErrors
        }
        throw "The local server did not start. See $serverErrors"
    }
    Write-Host "SMTP authentication succeeded. No email was sent during setup."
    Write-Host "Local site is running at http://127.0.0.1:8000 (PID $($server.Id))."
    Write-Host "Server logs: $serverOutput and $serverErrors"
    Write-Host "Stop the server with: Stop-Process -Id $($server.Id)"
} finally {
    Remove-Item Env:MAMS_SMTP_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:MAMS_SMTP_USERNAME -ErrorAction SilentlyContinue
    Remove-Item Env:MAMS_SMTP_FROM -ErrorAction SilentlyContinue
    Remove-Item Env:MAMS_SMTP_HOST -ErrorAction SilentlyContinue
    Remove-Item Env:MAMS_SMTP_PORT -ErrorAction SilentlyContinue
    Remove-Item Env:MAMS_EMAIL_BACKEND -ErrorAction SilentlyContinue
    $password = $null
    $securePassword.Dispose()
}
