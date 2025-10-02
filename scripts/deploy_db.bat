@echo off
REM =====================================================================
REM RAI-MINI :: Database deployment helper
REM Prerequisites: Python 3.10+ available in PATH, write permissions.
REM Optional: Run from an elevated prompt when deploying system-wide.
REM =====================================================================
setlocal
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.."

if not exist "server" mkdir "server"
if not exist "logs" mkdir "logs"

echo [deploy_db] Applying database migrations...
python server\init_db.py
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
    echo [deploy_db] Migration failed with code %ERR%.
    popd
    endlocal
    exit /b %ERR%
)

echo [deploy_db] Database ready at %CD%\server\apps.sqlite
popd
endlocal
