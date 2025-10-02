@echo off
REM =====================================================================
REM RAI-MINI :: Install the Windows service for the server API
REM Prerequisites: Administrator Command Prompt, Python 3.10+, NSSM installed.
REM   Define NSSM_PATH in .env (e.g. NSSM_PATH=C:\Tools\nssm.exe).
REM Optional: Define PYTHON_PATH in .env to override the python executable.
REM =====================================================================
setlocal enabledelayedexpansion
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.."

call :load_env

if not defined NSSM_PATH (
    echo [install_service] NSSM_PATH not set. Edit .env and add NSSM_PATH=C:\path\to\nssm.exe
    popd
    endlocal
    exit /b 1
)

if not exist "%NSSM_PATH%" (
    echo [install_service] NSSM executable not found at "%NSSM_PATH%".
    popd
    endlocal
    exit /b 1
)

set "PYTHON_CMD=%PYTHON_PATH%"
if not defined PYTHON_CMD set "PYTHON_CMD=python"

set "SERVICE_NAME=RAI-Server"
set "APP_ROOT=%CD%"
set "SERVICE_ARGS=-m rai.server.app"

if not exist "%APP_ROOT%\logs" mkdir "%APP_ROOT%\logs"

"%NSSM_PATH%" install %SERVICE_NAME% "%PYTHON_CMD%" %SERVICE_ARGS%
if errorlevel 1 goto :error

"%NSSM_PATH%" set %SERVICE_NAME% AppDirectory "%APP_ROOT%"
"%NSSM_PATH%" set %SERVICE_NAME% Start SERVICE_AUTO_START
"%NSSM_PATH%" set %SERVICE_NAME% AppStdout "%APP_ROOT%\logs\server-service.log"
"%NSSM_PATH%" set %SERVICE_NAME% AppStderr "%APP_ROOT%\logs\server-service.log"

echo [install_service] Service %SERVICE_NAME% installed successfully.
popd
endlocal
exit /b 0

:load_env
set "ENV_FILE=%CD%\.env"
if exist "%ENV_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /R "^NSSM_PATH=" "%ENV_FILE%"`) do (
        set "NSSM_PATH=%%~B"
    )
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /R "^PYTHON_PATH=" "%ENV_FILE%"`) do (
        set "PYTHON_PATH=%%~B"
    )
)
if defined NSSM_PATH set "NSSM_PATH=!NSSM_PATH:\"=!"
if defined PYTHON_PATH set "PYTHON_PATH=!PYTHON_PATH:\"=!"
exit /b 0

:error
echo [install_service] Failed to install service (errorlevel %ERRORLEVEL%).
popd
endlocal
exit /b %ERRORLEVEL%
