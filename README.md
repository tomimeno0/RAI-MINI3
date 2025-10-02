# RAI MINI3 – Setup para Windows 10/11

Asistente cliente-servidor minimalista para controlar aplicaciones en Windows. El objetivo de este documento es dejar el proyecto instalable y utilizable en equipos con Python 3.11 sin requerir privilegios de administrador.

## Requisitos

- Windows 10/11 (64 bits).
- [Python 3.11 (64 bits)](https://www.python.org/downloads/) instalado y agregado al `PATH`.
- Git para clonar el repositorio.
- PowerShell habilitado (viene por defecto en Windows; se usa para resolver accesos directos y listar apps UWP).

## Instalación express (4 comandos)

| Orden | Comando | ¿Qué hace? |
| --- | --- | --- |
| 1 | `git clone <URL_DEL_REPO> && cd RAI-MINI3` | Clona el proyecto y abre la carpeta. |
| 2 | `scripts\dev_setup.bat` | Crea `.venv`, instala `requirements.txt` y copia `.env.example` → `.env`. |
| 3 | `scripts\run_server.bat` | Lee `.env`, exporta variables y lanza `python server/app.py` en `127.0.0.1:5050`. Dejalo abierto. |
| 4 | `scripts\run_client_scan.bat` | Usa el mismo `.env`, ejecuta `client/scanner.py --full --send --url ...` y registra el primer escaneo. |

> 💡 Ejecutá el paso 4 en una **segunda terminal** (el servidor debe seguir corriendo).

## Checklist previo (fallas comunes)

- [ ] Python 3.11 (64 bits) responde `python --version` = `3.11.x`. <sub>Si no, `dev_setup` fallará creando `.venv`.</sub>
- [ ] `.env` actualizado con tu `RAI_SERVER_API_KEY` y la misma clave se usa en cliente/servidor. <sub>Si falta, verás respuestas 401 al escanear.</sub>
- [ ] PowerShell permite ejecutar comandos sin restricciones. <sub>Si está bloqueado, el escáner omitirá UWP; ver troubleshooting.</sub>
- [ ] (Opcional) `pywin32`/`pefile` instalados si querés metadatos avanzados. <sub>Sin ellos se emiten advertencias, pero el flujo continua.</sub>

## Dependencias

- `requirements.txt`: dependencias mínimas para producción (`Flask`).
- `dev-requirements.txt`: extras para desarrollo (por defecto sólo `pytest`).
- `scripts\dev_setup.bat` instala ambos archivos usando la venv `.venv`.

## Configuración de variables (.env)

El archivo `.env.example` incluye los valores necesarios. Copiálo a `.env` (lo hace `dev_setup` si no existe) y completá:

| Variable | Descripción |
| --- | --- |
| `RAI_SERVER_API_KEY` | Clave compartida que el servidor espera en el header `X-RAI-API-Key`. Debe coincidir con el cliente. |
| `DB_PATH` | Ruta del SQLite con el catálogo. Podés usar la relativa `rai\server\apps.sqlite` o una absoluta. |
| `CORS_ALLOWED_ORIGINS` | Lista separada por comas con los orígenes permitidos. Usá `*` sólo en entornos controlados. |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` / `RATE_LIMIT_BURST` | Limitador en memoria por IP. Poné `0` para deshabilitarlo. |
| `RAI_SERVER_URL` | Endpoint que usa el cliente para `POST /parse`. El script de escaneo deriva a `/apps/scan`. |

Opcional: `RAI_SERVER_HOST` y `RAI_SERVER_PORT` redefinen el bind de Flask (por defecto `127.0.0.1:5050`).

Las variables se cargan automáticamente al ejecutar los scripts `.bat`. Si `.env` no existe, los scripts continúan con valores por defecto y muestran una advertencia.

## Setup detallado

1. **Crear entorno**: `scripts\dev_setup.bat`
   - Detecta `py -3.11` o `python` en el `PATH`.
   - Crea `.venv` si no existe, actualiza `pip` e instala los requirements.
   - Copia `.env.example` a `.env` y te recuerda completarlo.
2. **Activar venv manualmente (opcional)**: `.\.venv\Scripts\Activate.ps1`
3. **Instalar extras opcionales**: `.\.venv\Scripts\pip install pywin32 pefile speechrecognition pyaudio` si necesitás audio o metadatos avanzados.

## Migraciones y base de datos

La base SQLite se crea automáticamente al iniciar el servidor o al ejecutar un escaneo. Si querés forzar una inicialización manual dentro de la venv:

```powershell
.\.venv\Scripts\python.exe -m rai.server.init_db
```

El archivo se guarda en `DB_PATH`. Podés respaldarlo o apuntarlo a otra carpeta desde `.env`.

## Arranque del servidor

- Script recomendado: `scripts\run_server.bat`
  - Lee `.env`, exporta variables y lanza `python server/app.py` dentro de la venv.
  - Logs en `logs\server.log`.
- Alternativa manual:

```powershell
setx RAI_SERVER_API_KEY "tu_clave"  # o usá $env:RAI_SERVER_API_KEY en la sesión actual
.\.venv\Scripts\python.exe server\app.py
```

El servidor queda escuchando en `http://127.0.0.1:5050`. Los endpoints protegidos (`/parse`, `/apps`, `/apps/scan`) devuelven `401` si la API key es incorrecta y `429` al superar el rate limit configurado.

## Primer escaneo del cliente

- Script recomendado: `scripts\run_client_scan.bat`
  - Vuelve a cargar `.env`, deriva `RAI_SERVER_URL` → `/apps/scan` y ejecuta `client/scanner.py --full --send`.
  - Muestra el JSON generado y reporta el resultado del POST.
- Alternativa manual:

```powershell
$env:RAI_SERVER_API_KEY = "tu_clave"
$env:RAI_SERVER_URL = "http://127.0.0.1:5050/parse"
.\.venv\Scripts\python.exe client\scanner.py --full --send --url http://127.0.0.1:5050/apps/scan
```

El cliente también puede levantar la UI interactiva con `.\.venv\Scripts\python.exe -m rai.client.main` (requiere que el servidor esté arriba).

## Troubleshooting

| Problema | Cómo se manifiesta | Solución |
| --- | --- | --- |
| **PowerShell restringido** | El script imprime `PowerShell no disponible` y el escáner omite apps UWP. | Ejecutá PowerShell como administrador una vez y corré `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. Luego reintentá el escaneo. |
| **pywin32 / pefile faltantes** | Logs con advertencias sobre metadatos y accesos directos no resueltos. | Son opcionales. Instalalos dentro de la venv (`pip install pywin32 pefile`) si necesitás hashes e íconos; de lo contrario podés ignorar el mensaje. |
| **Variables sin configurar** | `scripts\run_server.bat`/`run_client_scan.bat` muestran `[WARN] No se encontró .env` o el servidor responde `401`/`429`. | Completá `.env` con `RAI_SERVER_API_KEY` y revisá `RATE_LIMIT_*`. Los scripts deben leerse desde la carpeta raíz. |
| **Permisos de red bloqueados** | `client/scanner.py` registra `URLError` al enviar a `/apps/scan`. | Verificá que el firewall permita conexiones locales al puerto 5050. Podés cambiar el host con `RAI_SERVER_HOST`/`RAI_SERVER_PORT` en `.env` y reiniciar el server. |

## Tests

Para correr las pruebas unitarias (opcionales pero recomendadas):

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Esto valida utilidades clave del escáner en entornos Windows y CI.
