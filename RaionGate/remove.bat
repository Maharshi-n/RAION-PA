@echo off
rem Removes RaionGate: Raion can no longer read Tally. Double-click it, then click Yes.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
if exist "%ProgramFiles%\RaionGate\gate.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%ProgramFiles%\RaionGate\gate.ps1" -Remove
) else (
    echo RaionGate is not installed on this PC.
)
echo.
pause
