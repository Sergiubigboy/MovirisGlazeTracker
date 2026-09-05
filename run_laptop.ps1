# Run Glaze on a Windows laptop, for trying the flow out at a desk.
#
# Nothing here is Windows-specific beyond the setup: the app detects the
# platform itself and picks DirectShow for the cameras and the built-in
# Windows voice instead of V4L2 and espeak-ng. The Pi is unaffected.
#
#   .\run_laptop.ps1              one webcam as the eye, no scene camera
#   .\run_laptop.ps1 -Scene       second webcam as the scene camera
#   .\run_laptop.ps1 -Port 8080   different port

param(
    [switch]$Scene,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "creez mediul Python (o singura data)..." -ForegroundColor Cyan
    python -m venv .venv
    & ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    & ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
}

$args = @("-m", "glaze", "--port", "$Port")

Write-Host ""
Write-Host "  deschide in browser:  http://localhost:$Port/" -ForegroundColor Green
Write-Host "  pagina de teste:      http://localhost:$Port/test" -ForegroundColor Green
Write-Host "  Ctrl+C ca sa opresti" -ForegroundColor DarkGray
Write-Host ""

& ".venv\Scripts\python.exe" @args
