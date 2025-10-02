@echo off
REM =====================================================================
REM RAI-MINI :: Uninstall the Windows service for the server API
REM Prerequisites: Administrator Command Prompt. NSSM optional (used if available).
REM =====================================================================
setlocal enabledelayedexpansion
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.."

call :load_env
set "SERVICE_NAME=RAI-Server"

echo [uninstall_service] Stopping %SERVICE_NAME% if running...
sc stop "%SERVICE_NAME%" >nul 2>&1

if defined NSSM_PATH if exist "%NSSM_PATH%" (
    "%NSSM_PATH%" remove %SERVICE_NAME% confirm
) else (
    sc delete "%SERVICE_NAME%" >nul 2>&1
)

echo [uninstall_service] Service removal requested.
popd
endlocal
exit /b 0

:load_env
set "ENV_FILE=%CD%\.env"
if exist "%ENV_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /R "^NSSM_PATH=" "%ENV_FILE%"`) do (
        set "NSSM_PATH=%%~B"
    )
)
if defined NSSM_PATH set "NSSM_PATH=!NSSM_PATH:\"=!"
exit /b 0
