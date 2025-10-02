@echo off
REM =====================================================================
REM RAI-MINI :: Stop the Windows service for the server API
REM Prerequisites: Administrator Command Prompt.
REM =====================================================================
setlocal
set "SERVICE_NAME=RAI-Server"

echo [stop_service] Stopping %SERVICE_NAME%...
sc stop "%SERVICE_NAME%"
endlocal
