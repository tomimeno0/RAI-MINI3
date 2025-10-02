@echo off
setlocal
set "PROJECT_ROOT=%~dp0.."
pushd "%PROJECT_ROOT%" >nul

echo [INFO] Preparando entorno de desarrollo...

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_CMD=py -3.11"
) else (
    set "PYTHON_CMD=python"
)

echo [INFO] Usando %PYTHON_CMD% para crear el entorno virtual.

if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] No se pudo crear .venv. Verificá que Python 3.11 de 64 bits esté instalado.
        popd >nul
        exit /b 1
    )
) else (
    echo [INFO] Se reutiliza el entorno virtual existente.
)

set "VENV_PYTHON=.venv\Scripts\python.exe"

call "%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] No se pudo actualizar pip.
    popd >nul
    exit /b 1
)

call "%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Falló la instalación de requirements.txt.
    popd >nul
    exit /b 1
)

if exist "dev-requirements.txt" (
    call "%VENV_PYTHON%" -m pip install -r dev-requirements.txt
    if errorlevel 1 (
        echo [WARN] No se pudieron instalar las dependencias de desarrollo. Continuando...
    )
)

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        if errorlevel 0 (
            echo [INFO] Se creó .env a partir de .env.example. Completar valores antes de ejecutar.
        ) else (
            echo [WARN] No se pudo copiar .env.example. Creá .env manualmente.
        )
    ) else (
        echo [WARN] No se encontró .env.example. Creá .env manualmente.
    )
) else (
    echo [INFO] .env ya existe. No se modifica.
)

echo [OK] Entorno listo. Activá el venv con .venv\Scripts\Activate.ps1

popd >nul
endlocal
exit /b 0
