# RAI Mini (Windows)

Asistente por voz en modo CLI diseñado para Windows 10/11 sin privilegios de
administrador. El cliente escucha la hotword **"hola rai"** (o usa teclado como
fallback) y delega al servidor Flask la interpretación de órdenes para abrir,
cerrar, minimizar, maximizar y enfocar aplicaciones detectadas en el sistema.

## Estructura

```
rai/
├─ client/
│  ├─ main.py         # Loop principal (hotword/teclado → /parse → executor)
│  ├─ audio.py        # Manejo de hotword y captura de comandos (voz/teclado)
│  ├─ executor.py     # Ejecuta acciones en Windows (cmd + pywin32 opcional)
│  └─ scanner.py      # Escanea EXE y UWP, persiste en SQLite
├─ server/
│  ├─ app.py          # API Flask POST /parse
│  ├─ moduler.py      # Parser en español, usa la base de datos
│  └─ init_db.py      # Inicializador y escaneo manual
├─ server_db/         # Carpeta para la base SQLite generada en runtime
├─ logs/
│  ├─ client.log
│  └─ server.log
└─ README.md
```

## Requisitos

- Python 3.11
- Dependencias mínimas (`pip install -r requirements.txt` o manualmente):
  - `Flask`
  - `cohere` *(opcional, parser inteligente)*
  - `SpeechRecognition` *(opcional, habilita voz)*
  - `pyaudio` *(opcional, requerido por SpeechRecognition)*
  - `pywin32` *(opcional, requerido para minimizar/maximizar/enfocar UIs)*

Sin `SpeechRecognition`/`pyaudio` el cliente entra en **modo teclado** (Enter
para hablar). Sin `pywin32`, las acciones de ventana devuelven un mensaje de
“no disponible” y se registran en logs. Si falta `cohere`, el sistema cae al
parser por reglas (menos preciso pero funcional).

## Instalación rápida

```bash
python -m venv .venv
source .venv/Scripts/activate  # En PowerShell: .venv\Scripts\Activate.ps1
pip install Flask cohere SpeechRecognition pyaudio pywin32
```

Si `pyaudio` da error, instalá la rueda compatible con tu versión de Python o
omitilo y usá el modo teclado.

## Integración con Cohere

El parser principal usa la API de Cohere. Configuración sugerida:

- Instalá la dependencia: `pip install cohere`.
- Exportá la clave antes de levantar el servidor (PowerShell):
  ```powershell
  $env:COHERE_API_KEY = "tu_key"
  ```
- Modelo opcional via `COHERE_MODEL` (por defecto `command-r`).

Si la clave no está definida o la librería no está instalada, el parser
usa automáticamente el modo por reglas y lo informa en `logs/server.log`.

## Inicializar base de datos

El cliente ejecuta el scanner automáticamente al arrancar, pero podés forzarlo:

```bash
python -m rai.server.init_db
```

Esto crea/actualiza `server_db/apps.sqlite` con los EXE/UWP detectados y agrega
un catálogo base (WhatsApp, Discord, Chrome, Administrador de tareas). El
repositorio no incluye un archivo SQLite prellenado; se genera automáticamente
la primera vez que corras el escáner o el cliente.

## Correr el servidor

```bash
python -m rai.server.app
```

El servidor expone `POST /parse` en `http://127.0.0.1:5050/parse` y escribe
logs en `logs/server.log`.

## Correr el cliente

```bash
python -m rai.client.main
```

Flujo inicial:

1. Escaneo de aplicaciones y actualización de la base de datos.
2. Mensaje: `RAI listo. Decí 'hola rai' o presioná Enter para hablar.`
3. Hotword detectada (o Enter) → prompt `hola, ¿qué querés?`

Ejemplos de comandos:

- `abrime whatsapp`
- `cerrame chrome`
- `minimizame discord`
- `poné discord en foco`
- `abrime el administrador de tareas`
- `qué apps tengo`

## Troubleshooting

| Problema | Solución |
| --- | --- |
| **No detecta el micrófono** | Instalá `pyaudio`, verificá drivers. El cliente seguirá disponible por teclado. |
| **Acciones de minimizar/maximizar fallan** | Asegurate de tener `pywin32` instalado. Sin él, el cliente avisará que la función no está disponible. |
| **UWP no abre** | Verificá que el AUMID exista (`Get-StartApps`). Podés ejecutar `python -m rai.server.init_db` para refrescar el catálogo. |
| **Cerrar app no funciona** | Algunas apps requieren `/F`. El executor intenta primero cierre suave y luego forzado con aviso en logs. |
| **El servidor no responde** | Revisá que `python -m rai.server.app` esté corriendo. El cliente mostrará “No pude comunicarme con el parser…”. |

## Logs

- `logs/client.log`: eventos del loop, fallos de audio, resultados de ejecución.
- `logs/server.log`: peticiones al parser, errores de interpretación.

Ambos usan `RotatingFileHandler` (500 KB, 3 backups) para evitar crecimiento
ilimitado.

## Extensiones futuras

- HUD/Overlay gráfico.
- Entrenamiento de un modelo específico de hotword.
- Gestión de alias personalizados almacenados en la base.
