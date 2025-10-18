# Repository Guidelines

## Project Structure & Module Organization
RAI-MINI is a Windows-first automation assistant. `client.py` is the entry point that captures commands, resolves intents, and delegates to `hud.RAIHUD` for on-screen feedback. `actions.py` loads `apps.json`, normalizes catalog entries, and executes window control via `pygetwindow` and system processes. `hud.py` renders the notification overlay and handles transient status updates. Run `setup.py` when you need to (re)generate `apps.json` from an installed-app scan; it also writes `setup.log` with warnings. `NUEVOCLIENTE.PY` houses an experimental client variant—use it as a sandbox before merging UI changes back into `client.py`.

## Build, Test, and Development Commands
Create a virtual environment before hacking: `python -m venv .venv` and `.\.venv\Scripts\activate`. Install optional capabilities as needed with `pip install pywin32 pygetwindow speechrecognition keyboard requests openai cohere`. Launch the assistant locally with `python client.py`. Refresh the catalog via `python setup.py`, which prompts before touching `apps.json`. When iterating on HUD tweaks, you can run `python hud.py` directly to validate layout behaviour.

## Coding Style & Naming Conventions
Follow PEP 8: 4-space indents, snake_case for functions and variables, UpperCamelCase for classes. Keep user-facing strings in Spanish to match the existing voice prompts and HUD copy. Modules rely on `typing` annotations—maintain or extend them when adding APIs. Prefer `logging` over raw `print`, and route new actions through the existing `do_action` dispatcher so catalog lookups stay consistent.

## Testing Guidelines
There is no automated suite yet; add one under `tests/` using `pytest` when you introduce complex parsing or catalog logic. Use dependency injection or mocks for `pygetwindow`, `keyboard`, and network-bound services to keep tests hermetic. Validate setup flows manually on Windows: (1) run `python setup.py`, (2) confirm `apps.json` updates, and (3) trigger representative commands in `client.py` to ensure HUD feedback and action execution match expectations.

## Commit & Pull Request Guidelines
Match the existing git history by writing short, imperative commit subjects (e.g., “Adapt actions to dynamic catalog”). Explain the “why” in the body if context is non-obvious, and reference issue IDs when applicable. Pull requests should include: concise overview of behaviour changes, manual test evidence (commands run, HUD screenshots if UI changed), risk/rollback notes, and any configuration updates (e.g., regenerated `apps.json`). Ensure CI or local smoke checks succeed before requesting review.
