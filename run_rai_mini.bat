@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Cambiar al directorio de este script
cd /d "%~dp0"

set "PROJECT_DIR=%cd%"
set "APP_DIR=%PROJECT_DIR%"
set "REQ_FILE=%APP_DIR%\requirements.txt"
set "VENV_DIR=%PROJECT_DIR%\.venv"

if not exist "%REQ_FILE%" (
  echo No se encontro "%REQ_FILE%". Verifica que estas ejecutando este script desde la carpeta del proyecto.
  pause
  exit /b 1
)

if not exist "%APP_DIR%\setup.py" (
  echo No se encontro "%APP_DIR%\setup.py". Verifica la estructura del proyecto.
  pause
  exit /b 1
)

if not exist "%APP_DIR%\client.py" (
  echo No se encontro "%APP_DIR%\client.py". Verifica la estructura del proyecto.
  pause
  exit /b 1
)

echo [RAI-MINI] Verificando Python...
set "PY_CMD="
where py >nul 2>&1 && set "PY_CMD=py -3"
if not defined PY_CMD (
  where python >nul 2>&1 && set "PY_CMD=python"
)
if not defined PY_CMD (
  echo No se encontro Python en PATH. Instala Python 3.10+ y reintenta.
  pause
  exit /b 1
)

echo [RAI-MINI] Creando/activando entorno virtual...
if not exist "%VENV_DIR%\Scripts\python.exe" (
  %PY_CMD% -m venv "%VENV_DIR%"
  if errorlevel 1 goto :error
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 goto :error

set "PYTHON=%VENV_DIR%\Scripts\python.exe"

echo [RAI-MINI] Actualizando pip...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :error

echo [RAI-MINI] Instalando dependencias de %REQ_FILE% ...
"%PYTHON%" -m pip install -r "%REQ_FILE%"
if errorlevel 1 goto :error

rem Verificar PyAudio; si falta, intentar via pipwin (Windows)
"%PYTHON%" -c "import pyaudio" >nul 2>&1
if errorlevel 1 (
  echo [RAI-MINI] PyAudio no se pudo importar; intentando instalar con pipwin...
  "%PYTHON%" -m pip install --upgrade pipwin
  if errorlevel 1 goto :error
  "%PYTHON%" -m pipwin install pyaudio
  if errorlevel 1 goto :error
)

echo [RAI-MINI] Ejecutando setup para generar/actualizar apps.json ...
"%PYTHON%" "%APP_DIR%\setup.py"
if errorlevel 1 goto :error

echo [RAI-MINI] Iniciando cliente...
"%PYTHON%" "%APP_DIR%\client.py"
if errorlevel 1 goto :error

goto :eof

:error
echo.
echo [RAI-MINI] Ocurrio un error. Revisa los mensajes anteriores.
pause
exit /b 1
