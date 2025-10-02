@echo off
setlocal EnableDelayedExpansion
set "PROJECT_ROOT=%~dp0.."
pushd "%PROJECT_ROOT%" >nul

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] No se encontró .venv. Ejecutá scripts\dev_setup.bat primero.
    popd >nul
    endlocal
    exit /b 1
)

if exist ".env" (
    call :load_env ".env"
) else (
    echo [WARN] No se encontró .env. Se usarán los valores por defecto y las variables ya definidas.
)

set "SCAN_URL="
if defined RAI_SERVER_URL (
    set "SCAN_URL=!RAI_SERVER_URL!"
)
if not defined SCAN_URL (
    set "SCAN_URL=http://127.0.0.1:5050/parse"
)
set "SCAN_URL_RAW=!SCAN_URL!"
set "SCAN_URL=!SCAN_URL:/parse=/apps/scan!"
if /i "!SCAN_URL!"=="!SCAN_URL_RAW!" (
    if "!SCAN_URL:~-1!"=="/" (
        set "SCAN_URL=!SCAN_URL!apps/scan!"
    ) else (
        set "SCAN_URL=!SCAN_URL!/apps/scan!"
    )
)

echo [INFO] Ejecutando escaneo completo y enviando resultados a %SCAN_URL%
call ".venv\Scripts\python.exe" client\scanner.py --full --send --url "%SCAN_URL%"
set "EXIT_CODE=%ERRORLEVEL%"

popd >nul
endlocal & exit /b %EXIT_CODE%

:load_env
for /f "usebackq tokens=1* delims== eol=#" %%A in (%1) do (
    set "key=%%A"
    set "value=%%B"
    if defined value (
        for /f "tokens=* delims=" %%Z in ("!value!") do set "value=%%Z"
        set "value=!value:"=!"
    ) else (
        set "value="
    )
    set "!key!=!value!"
)
exit /b 0
