# Free Register Tool

A self-hosted account registration and management toolkit with a FastAPI backend, React frontend, plugin-based platform integrations, proxy support, mailbox adapters, and task scheduling.

This repository is intended for local deployment, development, and controlled automation workflows. Use it only in ways that comply with platform terms and applicable laws.

## Highlights

- Multi-platform plugin architecture under `platforms/`
- FastAPI backend with Web UI and REST API
- React + TypeScript frontend
- Task scheduling and batch execution
- Proxy pool and runtime state management
- Multiple mailbox integrations
- Built-in ChatGPT-related account flows and token handling
- Docker and local deployment options

## Stack

- Backend: FastAPI, Uvicorn, SQLModel, APScheduler
- Frontend: React, TypeScript, Vite, Ant Design
- Automation: Playwright, Camoufox
- Networking: `curl_cffi`, `httpx`
- Storage: SQLite

## Repository Layout

```text
api/          HTTP API routes
core/         Shared runtime, registry, scheduler, database helpers
frontend/     React frontend
platforms/    Platform plugins and platform-specific logic
services/     Background services and integration helpers
scripts/      Project utility scripts
tests/        Automated tests
tools/        Operational helper tools kept in the repo
docker/       Container entrypoint assets
main.py       Backend entrypoint
```

## Requirements

- Python 3.12+
- Node.js 18+
- Git
- A supported browser automation environment for Playwright/Camoufox

## Quick Start

### 1. Clone

```bash
git clone https://github.com/0311119/Free_RegisterTool.git
cd Free_RegisterTool
```

### 2. Create a Python environment

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
source .venv/bin/activate
```

### 3. Install backend dependencies

```bash
pip install -r requirements.txt
```

### 4. Install browser dependencies

```bash
python -m playwright install chromium
python -m camoufox fetch
```

### 5. Install frontend dependencies

```bash
cd frontend
npm install
npm run build
cd ..
```

### 6. Create local config

```bash
cp .env.example .env
```

Then edit `.env` for your local setup.

### 7. Start the backend

```bash
python main.py
```

Default API docs:

```text
http://localhost:8000/docs
```

## Frontend Development

```bash
cd frontend
npm install
npm run dev
```

Default dev URL:

```text
http://localhost:5173
```

## Configuration Notes

The project reads configuration from `.env` and persisted runtime settings.

Common categories:

- Server host and port
- Captcha solver settings
- Proxy settings
- Mailbox provider credentials
- External integration endpoints

Start from [`.env.example`](./.env.example).

## Testing

```bash
pytest tests/
```

Some repo scripts are operational helpers rather than automated tests. Prefer the `tests/` suite for repeatable verification.

## Docker

Basic Docker workflow:

```bash
docker-compose up -d
docker-compose logs -f
docker-compose down
```

## Development Notes

- Add new platform logic under `platforms/`
- Shared abstractions live under `core/`
- API routes live under `api/`
- Frontend pages live under `frontend/src/pages/`

## Tracked vs Local-Only Files

This repository is intentionally configured to keep local machine state out of version control.

Ignored local-only content includes:

- `.env`
- `data/`
- `logs/`
- `runtime/`
- `static/`
- `*.db`, `*.sqlite`, `*.sqlite3`
- runtime JSON state files
- local helper scripts and personal test files
- screenshots, debug output, caches, and local logs

If you add new machine-specific helpers, secrets, exports, or runtime data, keep them out of git as well.

## License

MIT. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).

## Acknowledgement

This repository builds on work from earlier upstream and forked projects in the same ecosystem. See project history and notices in the repository for attribution details.
