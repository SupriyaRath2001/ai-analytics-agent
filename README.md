# AI Analytics Agent

Ask questions about your BigQuery data in plain English, in a ChatGPT-style chat.

The agent runs behind [Open WebUI](https://docs.openwebui.com). It tells you what data you have,
and it answers questions about the data itself: it writes GoogleSQL, checks it, runs it on
BigQuery and shows the result as a table, a CSV download or a chart.

## Features

- **Explore questions**: "Which tables have customer data?", "What columns are in orders and what
  do they mean?" These are answered from a cache of your table metadata, with no BigQuery scan.
- **Analyse questions**: filters, joins across tables, group-bys and aggregates. The agent writes
  the SQL, checks it, runs it and shows the result. If a check or BigQuery rejects the query, the
  agent tries to fix it (up to 2 retries).
- **Tables, CSV downloads and charts**: small results appear as a table in the chat, and every
  result can be downloaded as CSV. Charts are PNG images, drawn when you ask for one.
- **Show SQL**: every data answer ends with a collapsible block that shows the query.
- **Read-only**: each query must be a single SELECT, pass a BigQuery dry run, use only allowed
  datasets and stay under a GB-billed limit.
- **Google login with admin approval**: users click "Continue with Google". New users wait until
  an admin approves them.

## How it works

```
Browser
   │ HTTPS
   ▼
 Caddy ── /downloads/* ───────────────┐
   │                                  │
   │ everything else                  │
   ▼                                  ▼
Open WebUI ── /v1/chat/completions ─▶ agent (FastAPI, port 8000, not published)
(chat, Google login)                  ├─▶ BigQuery: table metadata and queries
                                      ├─▶ LLM via LiteLLM: fast and smart model
                                      └─▶ KB cache (DuckDB), CSV and PNG files
```

At startup the agent reads the metadata of every allowed table and writes a short summary of each
new or changed table. The chat works right away; summaries fill in in the background. Open WebUI
shows a single model, `analytics-agent`.

## Quick start (local)

> **Want a demo first?** [docs/demo.md](docs/demo.md) runs the whole app on a small Google Cloud
> VM against Google's public demo shop data in about 20 minutes, with test questions to try.

You need Docker with Compose, an API key for an LLM provider, and a Google Cloud service-account
key with the roles **BigQuery Data Viewer** and **BigQuery Job User**.

```bash
git clone https://github.com/SupriyaRath2001/ai-analytics-agent.git
cd ai-analytics-agent
cp .env.example .env
cp config/config.yaml.example config/config.yaml
```

Edit `.env`:

- `FAST_MODEL`, `SMART_MODEL`, `LLM_API_KEY`: [LiteLLM](https://docs.litellm.ai) model strings
  and your provider's key. The key is used for both models; to mix providers, see the note in
  `.env.example`.
- `GCP_PROJECT`: the project that holds your BigQuery data.
- Save the service-account key as `secrets/sa.json` and set
  `GOOGLE_APPLICATION_CREDENTIALS=/app/secrets/sa.json`.
- `AGENT_API_KEY` and `WEBUI_SECRET_KEY`: replace `change-me` with long random strings, for
  example from `openssl rand -hex 32`.
- Leave `DOMAIN` and `WEBUI_URL` as `http://localhost`.

Then start it:

```bash
docker compose up -d
```

The first start builds the agent image and downloads Open WebUI, which takes a few minutes.
Open http://localhost and create an account. **The first account becomes the admin.** Google login
is optional on your own machine: without `GOOGLE_CLIENT_ID`, sign up with an email and password.
To try Google login locally, add `http://localhost/oauth/google/callback` as a redirect URI of
your OAuth client.

Follow the agent's startup with `docker compose logs -f agent`.

To run it on a server with your own domain and HTTPS, see
[docs/deploy-vm.md](docs/deploy-vm.md).

## How answers are shown

| Result | What the chat shows |
|---|---|
| 0 rows | "No rows matched", plus the assumptions and the SQL |
| 1–20 rows and ≤ 12 columns | full table in chat, a 1–2 line takeaway, a CSV download link |
| more than 20 rows, or more than 12 columns | 5-row preview, row/column count, CSV download link |
| more than `max_rows_fetch` (50,000) | nothing downloaded; the user is told the row count and how to narrow the question |
| chart needed | PNG chart in chat, plus a download link for it |

Every data answer ends with a collapsible **Show SQL** block. When a question is unclear, the agent
asks only if a wrong guess would mislead; otherwise it guesses and states its assumptions.
Download files are deleted after 24 hours. All of these limits can be changed in
`config/config.yaml`.

## Configuration

| File | What it holds |
|---|---|
| `.env` (copy of [`.env.example`](.env.example)) | Secrets and per-deployment values: LLM models and key, GCP project, timezone, domain, Open WebUI keys, Google OAuth client. Never commit it. |
| `config/config.yaml` (copy of [`config/config.yaml.example`](config/config.yaml.example)) | The BigQuery connection (dataset include/exclude lists, GB billed per query, timeout, row limit), the two LLM tiers and the output limits. `${VAR}` values are read from `.env`. |
| `config/business_context.md` (optional, copy of [`config/business_context.md.example`](config/business_context.md.example)) | Your metric definitions, business words and calendar rules. The agent adds them to its prompts. |

The fast model does the easy steps (routing, table summaries, chart specs, short answers); the
smart model writes and fixes SQL.

After changing `.env`, run `docker compose up -d`. After changing a file in `config/`, run
`docker compose restart agent`. The table list is only refreshed on restart.

Open WebUI saves most of its own settings in its database on first start, so some later `.env`
changes don't reach it. See
[Changing settings later](docs/deploy-vm.md#changing-settings-later).

## Security

v1 keeps security minimal on purpose:

- Queries are read-only (see Features). The agent's port is not published, so it can only be
  reached inside the Docker network; Open WebUI calls it with `AGENT_API_KEY`.
- Every approved user can ask about every allowed dataset. There are no per-user access rules
  and no PII masking. Limit what the agent sees with `include_datasets` / `exclude_datasets` and
  the service account's BigQuery roles.
- Download links work without logging in. Their names are random and they expire after 24 hours.

## Adding a database

v1 supports BigQuery only. The connector interface is pluggable: a new database means one
connector class plus one line in `backend/connectors/registry.py`. See
[docs/adding-a-database.md](docs/adding-a-database.md).

## Project layout

```
backend/
  main.py          FastAPI app: /health, /v1/models, /v1/chat/completions, /downloads
  config.py        load and validate config/config.yaml
  llm.py           LiteLLM wrapper with a fast and a smart model
  connectors/      database connectors, read-only SQL check, registry
  bootstrap/       KB cache (DuckDB) and the startup crawler
  agent/           chat history, prompts, the agent itself
  services/        CSV/PNG exports, markdown tables, charts
config/            example config and business context
docs/              design decisions, VM deployment, adding a database
tests/             pytest, no network
Dockerfile, docker-compose.yml, Caddyfile
```

The design decisions and module contracts are in [docs/DECISIONS.md](docs/DECISIONS.md).

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

pytest -q
ruff check .
ruff format .
```

Tests use fakes: no network, no real BigQuery and no real LLM. CI runs `ruff check`,
`ruff format --check` and `pytest` on every push and pull request.

## Roadmap

Not in v1: MySQL connector, cross-database joins (DuckDB can attach both), embeddings, per-user
access rules, PII masking, audit log, schema refresh without restart, Cloud Run deployment,
interactive charts, Excel export.

## License

[Apache License 2.0](LICENSE)
