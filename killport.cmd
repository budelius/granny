@echo off
REM Quick port killer - just pass the port number
REM Usage: killport 3369

if "%1"=="" (
    echo Usage: killport ^<port^>
    echo Example: killport 3369
    exit /b 1
)

powershell.exe -ExecutionPolicy Bypass -File "%~dp0granny.ps1" -Port %1 -Kill -Force
