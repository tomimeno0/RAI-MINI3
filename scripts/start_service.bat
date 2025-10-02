@echo off
REM =====================================================================
REM RAI-MINI :: Start the Windows service for the server API
REM Prerequisites: Administrator Command Prompt.
REM =====================================================================
setlocal
set "SERVICE_NAME=RAI-Server"

echo [start_service] Starting %SERVICE_NAME%...
sc start "%SERVICE_NAME%"
endlocal
