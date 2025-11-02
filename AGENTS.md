# Repository Guidelines

## Project Structure & Module Organization
RAI-MINI centers on `client.py`, which captures comandos de voz, resuelve intents y coordina el HUD (`hud.RAIHUD`) para feedback visual. Automations live in `actions.py`, reading `apps.json`, normalizing entries, and issuing window/process controls. `hud.py` renders the overlay UI and handles transient mensajes. Run `setup.py` to regenerate `apps.json` from an installed-app scan; review `setup.log` for warnings. `NUEVOCLIENTE.PY` exists as a sandbox for experimental HUD or flujo changes before porting to `client.py`. Ancillary assets, logs, and catalog files sit beside these modules at repo root.

## Build, Test, and Development Commands
Initialize a virtualenv with `python -m venv .venv` and activate via `.\.venv\Scripts\activate`. Install optional extras as needed: `pip install pywin32 pygetwindow speechrecognition keyboard requests openai cohere`. Launch the assistant with `python client.py`. For HUD-only tweaks, run `python hud.py` to iterate on layout. Refresh the catalog using `python setup.py`; the script will prompt before touching `apps.json`.

## Coding Style & Naming Conventions
Follow PEP 8: 4-space indentation, `snake_case` for funciones/variables, `UpperCamelCase` for clases. Preserve or extend type annotations across modules. Prefer `logging` over bare `print` so output routes through the central logger. Keep user-visible cadenas in Spanish to match existing prompts. New automations should flow through the `do_action` dispatcher to reuse catalog lookups and telemetry.

## Testing Guidelines
There is no automated suite yet; when adding lógica compleja or catalog transforms, create `pytest` suites under `tests/`. Mock `pygetwindow`, `keyboard`, and network clients so tests remain hermetic. Manual validation on Windows remains essential: (1) execute `python setup.py`, (2) confirm `apps.json` updates, (3) trigger representative comandos via `client.py` and observe HUD/log feedback.

## Commit & Pull Request Guidelines
Emulate the existing history by using short, imperative commit subjects (e.g., “Ajusta HUD para mensajes encadenados”). Include context in the body when the motivo is non-obvious and cross-link issue IDs when available. Pull requests should summarize behaviour changes, list manual test evidence (commands run, HUD screenshots if UX changed), and call out rollback considerations. Ensure local smoke checks or CI equivalents pass before requesting review.

## Security & Configuration Tips
Keep secrets such as `COHERE_API_KEY` out of source—load them from environment variables or a secure vault. Inspect `setup.log` after regenerating the catalog to catch missing installations early. When scripting elevated operations, prototype in a controlled VM or secondary profile before deploying on a primary workstation.
