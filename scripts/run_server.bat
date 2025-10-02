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

echo [INFO] Iniciando servidor Flask...
call ".venv\Scripts\python.exe" server\app.py
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
