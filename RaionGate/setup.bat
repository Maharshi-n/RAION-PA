@echo off
rem RaionGate setup: lets Raion read Tally (it can never change Tally). Double-click it, then click Yes.
if not exist "%~dp0gate.ps1" (
    echo Put gate.ps1 in the same folder as this file, then run it again.
    pause
    exit /b
)
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gate.ps1" -Install
echo.
pause
