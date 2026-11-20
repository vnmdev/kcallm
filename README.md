# KcaLLM

A self-hosted food diary that keeps the precision of a personal nutrition spreadsheet while making capture quick. Type "180g potato and 2 mango packs" or attach a photo of your scale, review what the app understood, then confirm.

## How it works

**The AI suggests, your catalogue decides, and Python does the maths.**

- **Language model:** interprets free text or a photo and proposes foods from *your* approved catalogue, with quantities. It never supplies nutrition for existing foods and cannot save anything by itself.
- **Catalogue:** the foods you have reviewed and approved, each with an explicit nutrition basis (for example per 100 g or per pack).
- **Backend:** FastAPI and Pydantic validate every proposal. Python's `Decimal` calculates nutrition from the catalogue, and SQLite stores the result only after you confirm the preview.

Manual logging works without any AI provider.

## Features

- **Text and photo capture:** describe a meal or attach a scale photo, then resolve any ambiguous matches before previewing.
- **Label extraction:** paste a nutrition-label screenshot to create an editable catalogue draft. Values such as `<0.5g` are treated as unknown, not as exact amounts.
- **Exact numbers:** decimal arithmetic throughout. Unknown nutrients stay unknown (`null`) rather than becoming zero.
- **Safe saving:** batch saves are transactional, idempotent and revision-checked, so a retried confirmation cannot log a meal twice.
- **History you can trust:** each diary entry keeps a snapshot of the food as it was, so editing the catalogue never rewrites past days.
- **Drafts survive failures:** unfinished entries, including photos, persist in the browser until you confirm or cancel.
- **Backup and restore:** a full JSON export, validated before any restore replaces data.

## Stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.13, FastAPI, Pydantic, SQLite, httpx |
| Frontend | React, TypeScript, Vite |
| AI providers | ChatGPT (account sign-in) or any OpenAI-compatible llama.cpp server |
| Testing | pytest, Playwright, Ruff, mypy (strict) |
| Deployment | Docker |

## Run locally

Requirements: [uv](https://docs.astral.sh/uv/), Node.js 24+ and npm.

```sh
uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend run build
uv run uvicorn kcallm.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open http://127.0.0.1:8000. The catalogue and diary start empty. Synthetic example foods are in `examples/catalogue.json`.

For configuration, copy `.env.example` to `.env` and run with `uv run --env-file .env ...`. For frontend development, run `npm --prefix frontend run dev` alongside the backend; Vite proxies `/api` to port 8000.

## AI providers

- **ChatGPT:** connect through Settings using the account sign-in flow, then choose a model. No API key is stored, and the app never switches to paid usage on its own.
- **llama.cpp:** set `KCALLM_LLAMA_BASE_URL` to an OpenAI-compatible endpoint, for example `http://127.0.0.1:8080/v1`.

Providers receive the text or photo plus a compact list of catalogue names and units. Existing nutrition facts and diary history are not sent.

## Tests

```sh
uv run pytest -q
uv run ruff check backend scripts
uv run mypy
npm --prefix frontend run build
npm --prefix frontend run test:ui
```

Tests use temporary databases and mocked providers. Live model checks are opt-in through `scripts/evaluate.py`, which runs against a synthetic catalogue and never writes to your diary.

## Code map

| Path | Responsibility |
| --- | --- |
| `backend/kcallm/models.py` | Validated data shapes and decimal serialisation |
| `backend/kcallm/calculation.py` | Pure portion conversion and nutrient totals |
| `backend/kcallm/storage.py` | SQLite transactions, snapshots, revisions and restore |
| `backend/kcallm/app.py` | HTTP routes, origin checks and frontend serving |
| `backend/kcallm/ai/` | Provider sign-in, model transport and proposal validation |
| `frontend/src/` | Views, API client and local draft persistence |

## Status

A personal project in active use and development. It was built with coding agents (Codex and Claude Code) from my own product design and constraints, and I'm working through the codebase to review and extend it. It is single-user by design: there is no account system, and access is expected to be restricted at the network level.
