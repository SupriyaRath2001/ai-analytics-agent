# v1 build plan and decisions

This file is the source of truth for v1. If code and this file disagree, fix one of them on purpose.

## 1. What v1 does

People ask questions about BigQuery data in plain English in a ChatGPT-style chat (Open WebUI).

There are two kinds of questions:

1. **Explore**: "Which tables have customer data?", "What columns are in orders and what do they mean?"
   These are answered from the KB cache, with **no BigQuery scan**.
2. **Analyse**: filters, joins across tables, group-bys and aggregates. The agent writes GoogleSQL,
   checks it, runs it on BigQuery and shows the result.

What the user gets back:

| Result | What the chat shows |
|---|---|
| 0 rows | "No rows matched", plus the assumptions and the SQL |
| 1–20 rows and ≤ 12 columns | full table in chat, a 1–2 line takeaway, a CSV download link |
| more than 20 rows, or more than 12 columns | 5-row preview, row/column count, CSV download link |
| more than `max_rows_fetch` (50,000) | nothing downloaded; the user is told the row count and how to narrow the question |
| chart needed | PNG chart in chat, plus a download link for it |

Every data answer ends with a collapsible **Show SQL** block.

## 2. Decisions

| Topic | Decision |
|---|---|
| Databases | **BigQuery only** in v1: one GCP project, all datasets (with optional include/exclude lists). The connector interface stays pluggable, so MySQL later means one new file plus one registry line. |
| Environments | One deployment = one config = one project. Dev and prod are separate deployments. |
| Cross-database joins | Not in v1. |
| Users | 50+, English. The service is a thin API; BigQuery and the LLM do the heavy work. |
| LLM | LiteLLM; the provider is chosen later. Two tiers: `fast_model` (routing, curation, chart specs, takeaways, metadata answers, Open WebUI background tasks) and `smart_model` (writing and fixing SQL). There's no model picker in the UI: Open WebUI shows one model, `analytics-agent`. |
| Framework | Plain Python + LiteLLM + Pydantic + FastAPI. No LangChain/LangGraph. |
| Embeddings | None in v1. The fast model picks tables from a short catalog (name + one-line summary). With ≤ 15 tables, every table is passed and the pick step is skipped. |
| Startup | The crawler runs in a background thread. It saves raw metadata first (seconds), then writes LLM summaries for new or changed tables. The API serves right away. |
| Schema refresh | Only on restart. |
| KB cache | DuckDB file `data/kb_cache.duckdb`, with a SHA-256 hash per table. Unchanged tables are skipped: no profiling, no LLM cost. |
| Business context | Optional `config/business_context.md`. It's loaded at startup, stored in the KB and added to SQL and metadata prompts (capped at 20,000 characters). |
| Reading metadata | BigQuery **API** (`list_datasets`, `list_tables`, `get_table`). It's free and includes nested fields, descriptions, partitioning, clustering and row counts. The original spec used INFORMATION_SCHEMA queries; the API gives the same data with zero query cost and simpler code. |
| Profiling | One query per table: `APPROX_TOP_COUNT` on top-level STRING columns. Values are kept only when a column has ≤ 15 distinct values. Profiling jobs are capped at 1 GiB billed; failures are logged and skipped. |
| Read-only safety | 1) SQLGlot: exactly one statement, which must be a SELECT/WITH query. 2) BigQuery dry run: valid syntax, `statement_type == "SELECT"`, every referenced table is in the configured project and an allowed dataset, and bytes ≤ `max_bytes_billed_gb`. The same byte limit is set as `maximum_bytes_billed` on the real job. |
| Self-correction | Up to **2 retries**, each feeding the exact error back to the smart model. After that, the user gets a plain-English failure message. |
| Clarifying questions | The agent decides. It asks only when a wrong guess would mislead (unclear metric, a time range that changes the answer, a calendar choice). Otherwise it guesses and states its assumptions. |
| Row counts | `total_rows` from the finished BigQuery job, so no extra COUNT query. Rows are downloaded only when `total_rows ≤ max_rows_fetch`. |
| Charts | Only when the user asks or the question clearly needs one. Otherwise the answer ends with *"Want a chart of this? Just ask."* A ChartSpec (JSON from the fast model) feeds deterministic Matplotlib/Seaborn code. Never `exec` LLM code. Data is aggregated in SQL first. |
| Downloads | CSV (UTF-8 with BOM so Excel opens it cleanly). Files live in `exports/` (a Docker volume) with random 32-hex names and are deleted after 24 h. Served at `/downloads/<name>`. PNGs display inline; `?download=1` forces a download. |
| Chat state | Nothing is stored server-side. Follow-ups use the chat history Open WebUI sends; the last SQL is in the previous answer's "Show SQL" block. |
| Formats | Numbers `1,234,567`; dates `YYYY-MM-DD`. Several currencies may exist: never convert, never add amounts across currencies. |
| Timezone | `timezone` in config (from `TZ`, default UTC). Prompts give the model today's date in that zone and tell it to use `CURRENT_DATE('<tz>')`. |
| Hosting | One small VM with Docker Compose running the agent, Open WebUI and Caddy (HTTPS). Caddy sends `/downloads/*` to the agent and everything else to Open WebUI. Same origin, so relative links work. |
| Login | Open WebUI "Continue with Google". New users stay pending until an admin approves them (`DEFAULT_USER_ROLE=pending`). |
| Security in v1 | Minimal on purpose: the read-only checks above, an optional `AGENT_API_KEY` between Open WebUI and the agent, and the agent port is not published. No PII masking, no per-user access rules. |
| Logging | Standard Python logging to the console. No audit DB, no tracing. |
| Secrets | Only in `.env` (git-ignored). Never in code, examples or commits. |

## 3. Request flow

```
Open WebUI ──POST /v1/chat/completions──▶ AnalyticsAgent.run(messages)  (yields markdown chunks)
   1. clean the chat history, take the latest user text
   2. Open WebUI background task ("### Task:" prompt for titles/tags/follow-ups)
        → answer with the fast model directly, never touch BigQuery
   3. route (fast model) → intent: metadata | data | help, wants_chart, uses_previous_result
   4. metadata → KB lookup → fast model writes the answer (markdown). No BigQuery.
   5. data     → pick tables (fast) → write SQL (smart; may return a clarifying question instead)
                → check (sql_guard + dry run) → retry ≤ 2 → run → render table / CSV / chart
   6. help     → what the agent can do + 3 example questions built from real table names
```

Progress is streamed first as short blockquote lines (`> Finding the right tables…`), then the answer.

## 4. Repository layout

```
backend/
  config.py                 load + validate config.yaml (done)
  llm.py                    LiteLLM wrapper: complete(), complete_json() (done)
  main.py                   FastAPI app: lifespan, /health, /v1/models, /v1/chat/completions, /downloads
  connectors/
    base.py                 BaseDBConnector, ColumnMeta, TableMeta, QueryResult, QueryError (done)
    sql_guard.py            SQLGlot read-only check
    bigquery_connector.py   BigQueryConnector
    registry.py             type name → connector class
  bootstrap/
    kb_cache.py             DuckDB KB store
    curator.py              startup crawl, hashing, profiling, LLM summaries, business context
  agent/
    history.py              clean Open WebUI history, find the last SQL
    prompts.py              all prompt text in one place
    router.py               AnalyticsAgent: routing, metadata answers, SQL loop, rendering
  services/
    exports.py              ExportStore: save CSV/PNG, URLs, 24 h cleanup
    renderer.py             markdown tables, previews, SQL block
    charts.py               ChartSpec + Matplotlib/Seaborn PNG rendering
config/                     config.yaml.example, business_context.md.example
tests/                      pytest, no network
docs/                       this file, deploy-vm.md, adding-a-database.md
Dockerfile, docker-compose.yml, Caddyfile, .env.example, .github/workflows/ci.yml
```

## 5. Contracts

Signatures below are binding. Everything else (private helpers, file splits) is up to the module owner.

### connectors/base.py (done)
See the file: `ColumnMeta`, `TableMeta` (with `ddl_hash()`), `QueryResult`, `QueryError`, `BaseDBConnector`
(`dialect`, `connect`, `fetch_raw_metadata`, `sample_column_values`, `dry_run_or_validate`,
`execute_query`, `sql_hints`, `close`).

### connectors/sql_guard.py
```python
def check_read_only(sql: str, dialect: str) -> tuple[bool, str]:
    """(True, "") when sql is exactly one read-only SELECT/WITH query, else (False, reason)."""
```
Reject: parse errors, zero or more than one statement, anything that isn't a query, and any node that
writes or locks (INSERT/UPDATE/DELETE/MERGE/CREATE/DROP/ALTER/commands, `SELECT … INTO`,
`FOR UPDATE`). Also reject the functions `sleep`, `benchmark` and `load_file` (useful once MySQL arrives).
Trailing semicolons and whitespace are fine.

### connectors/bigquery_connector.py
```python
class BigQueryConnector(BaseDBConnector):
    type_name = "bigquery"
    dialect -> "bigquery"
```
- `connect()`: `bigquery.Client(project=..., location=...)`, using the service-account key at
  `credentials_path` when set, otherwise Application Default Credentials.
- Allowed datasets: `include_datasets` (empty = all) minus `exclude_datasets`. `exclude_tables` holds
  `"dataset.table"` entries.
- `fetch_raw_metadata()`: one `TableMeta` per table/view, with `qualified_name = "project.dataset.table"`.
  Nested RECORD fields appear as the parent column plus `parent.child` paths. The partition column has
  `is_partition=True`; for ingestion-time partitioning, add a pseudo column `_PARTITIONTIME`.
  `clustering_order` is 1-based. A table that fails to load is logged and skipped.
- `sample_column_values(table, columns, limit=15)`: one `APPROX_TOP_COUNT` query over the top-level,
  non-repeated STRING columns in `columns`, capped at 1 GiB billed. Returns only the columns with ≤ `limit`
  values. Any error → log and return `{}`.
- `dry_run_or_validate(query)`: the checks in section 2. Returns `(True, "Dry run OK, will scan 1.2 GB.")`
  or `(False, "<clear reason>")`. Messages are plain enough to show the SQL writer.
- `execute_query(query, max_rows=None)`: `max_rows` defaults to `config.max_rows_fetch`. Waits up to
  `query_timeout_sec` and cancels the job on timeout. When `total_rows > max_rows`, it returns
  `QueryResult(df=None, too_large=True, ...)` without downloading. Otherwise it downloads with
  `to_dataframe(create_bqstorage_client=False)`. Google API errors become `QueryError(message)`.
- `sql_hints()`: GoogleSQL rules for the SQL writer: backticked `project.dataset.table` names, UNNEST for
  REPEATED fields, dot paths for RECORD fields, filter on the partition column when there is one,
  SAFE_DIVIDE, no `SELECT *` on wide tables, ORDER BY for top-N and time series, and TIMESTAMPs are UTC
  (use `DATE(ts, tz)` to group by local day).

### connectors/registry.py
```python
CONNECTOR_TYPES: dict[str, type[BaseDBConnector]]          # {"bigquery": BigQueryConnector}
def create_connector(config: ConnectionConfig) -> BaseDBConnector   # ValueError for unknown types, naming the known ones
def build_connectors(configs: list[ConnectionConfig]) -> dict[str, BaseDBConnector]
    # enabled connections only, connect() called, keyed by config.name
```

### services/exports.py
```python
class ExportStore:
    def __init__(self, export_dir: str | Path, public_base_url: str = "", ttl_hours: int = 24): ...
    def save_csv(self, df: pd.DataFrame) -> str        # file name "<32 hex>.csv", UTF-8 with BOM, no index
    def save_png(self, data: bytes) -> str             # file name "<32 hex>.png"
    def url_for(self, file_name: str) -> str           # f"{public_base_url}/downloads/{file_name}"
    def path_for(self, file_name: str) -> Path | None  # None when the name is malformed, missing or expired
    def cleanup(self) -> int                           # delete expired files, return how many
```

### services/renderer.py
```python
class RenderedResult(BaseModel):
    markdown: str     # the table (or preview) plus the download-link line
    inline: bool      # True when every row is shown in the chat
    csv_file: str     # file name in the ExportStore

def format_cell(value: object, column_name: str) -> str
def df_to_markdown(df: pd.DataFrame) -> str
def render_result(df: pd.DataFrame, total_rows: int, store: ExportStore, output: OutputConfig) -> RenderedResult
def render_too_large(total_rows: int, max_rows: int) -> str
def render_sql_block(sql: str) -> str                   # <details><summary>Show SQL</summary> + ```sql block
def render_assumptions(assumptions: list[str]) -> str   # "" when empty, else "_Assumed: a; b._"
```
Number formatting: thousands separators for numbers, except integer columns whose names look like ids,
years or codes (`id`, `*_id`, `*id`, `year`, `code`, `zip`, `pin`, `phone`). Floats get at most 2
decimals. Dates are ISO and timestamps `YYYY-MM-DD HH:MM:SS`. None/NaN → empty. Escape `|` and newlines
in strings. Download line: `[⬇️ Download CSV (1,234 rows)](url)`.

### services/charts.py
```python
class ChartSpec(BaseModel):
    chart_type: Literal["bar", "line", "scatter", "hist", "box", "heatmap", "pie"]
    x_col: str
    y_col: str | None = None
    hue_col: str | None = None
    value_col: str | None = None     # heatmap cell values
    title: str
    x_label: str | None = None
    y_label: str | None = None
    horizontal: bool = False         # bar charts only
    top_n: int | None = None         # bar/pie: keep the top N; pie groups the rest as "Other"
    sort: Literal["none", "asc", "desc"] = "none"

def check_spec(spec: ChartSpec, df: pd.DataFrame) -> list[str]   # problems; empty list = fine
def render_chart(df: pd.DataFrame, spec: ChartSpec) -> bytes      # PNG bytes
```
Use the Agg backend, a seaborn whitegrid theme, 10×6 in at 150 dpi and a white background. Bars use
`estimator="sum", errorbar=None`, so repeated x values are summed, not averaged. Pies default to the top
8 + "Other". Bar charts with more than 30 categories keep the top 30.

### bootstrap/kb_cache.py
```python
class ColumnInfo(BaseModel):
    name: str; data_type: str; mode: str | None; description: str; meaning: str
    sample_values: list[str]; is_partition: bool; clustering_order: int | None

class TableInfo(BaseModel):
    connection: str; qualified_name: str; schema_name: str; table_name: str; table_type: str
    description: str; summary: str; tags: list[str]; row_count: int | None; size_bytes: int | None
    column_count: int; curated: bool

class TableDetail(TableInfo):
    columns: list[ColumnInfo]

class KBCache:
    def __init__(self, path: str | Path): ...   # creates the file/tables; one connection + a threading.Lock
    def upsert_raw(self, table: TableMeta) -> bool
        # Save raw metadata. Returns True when the table is new, its ddl_hash changed or it was never
        # curated. A changed hash clears the old summary/meanings/samples. An unchanged hash only
        # refreshes row_count/size_bytes.
    def save_curation(self, connection: str, qualified_name: str, summary: str, tags: list[str],
                      column_meanings: dict[str, str], sample_values: dict[str, list[str]]) -> None
        # sets curated = True
    def remove_missing(self, connection: str, keep: set[str]) -> int
    def list_tables(self, connection: str | None = None) -> list[TableInfo]
    def get_table(self, qualified_name: str, connection: str | None = None) -> TableDetail | None
    def find_tables(self, names: list[str], connection: str | None = None) -> list[TableDetail]
        # matches qualified name, "dataset.table" or a bare table name, case-insensitive, de-duplicated
    def set_business_context(self, text: str) -> None
    def get_business_context(self) -> str
    def stats(self) -> dict[str, int]   # {"tables": n, "curated": m}
    def close(self) -> None
```

### bootstrap/curator.py
```python
class TableCuration(BaseModel):
    summary: str                      # 1–2 plain-English sentences
    tags: list[str] = []              # up to 5 lowercase domain words
    columns: dict[str, str] = {}      # column name → meaning in ≤ 15 words

class Curator:
    status: str                       # "idle" | "running" | "done" | "failed"
    def __init__(self, connectors: dict[str, BaseDBConnector], kb: KBCache, llm: LLMClient,
                 business_context_path: str | None = None, max_workers: int = 4): ...
    def run(self) -> None                         # the whole startup pass; logs; never raises
    def load_business_context(self) -> None
    def sync_raw_metadata(self) -> list[TableMeta]  # upsert all, remove dropped tables, return tables needing curation
    def curate_table(self, table: TableMeta) -> None  # profile → fast LLM → save_curation
```
If the LLM fails for a table, log it and leave the table uncurated so the next start retries it.

### agent/history.py
```python
def message_text(message: dict) -> str            # content may be a string or a list of parts
def latest_user_text(messages: list[dict]) -> str
def clean_history(messages: list[dict], max_messages: int = 10) -> list[dict]
    # Earlier turns only (not the latest user message), user/assistant roles. Assistant text is stripped
    # of tables, images, links and progress lines; SQL is kept as "Previous SQL:\n```sql ...```".
def last_sql(messages: list[dict]) -> str | None
def is_openwebui_task(text: str) -> bool          # Open WebUI background prompts start with "### Task:"
```

### agent/router.py
```python
class AnalyticsAgent:
    def __init__(self, config: AppConfig, connectors: dict[str, BaseDBConnector], kb: KBCache,
                 llm: LLMClient, store: ExportStore): ...
    def run(self, messages: list[dict]) -> Iterator[str]   # markdown chunks: progress lines, then the answer
```
v1 uses the first connector. It never raises to the API: every failure becomes a friendly message.

### main.py
- `GET /health` → `{"status": "ok", "curation": curator.status, "kb": kb.stats()}`
- `GET /v1/models` → OpenAI list format with one model, id `analytics-agent`
- `POST /v1/chat/completions` → OpenAI-compatible, streaming (SSE `chat.completion.chunk`, ending with
  `data: [DONE]`) and non-streaming. Bearer auth when `agent_api_key` is set. Unknown request fields
  are ignored.
- `GET /downloads/{file_name}` → the file from ExportStore, 404 when missing or expired. CSVs download as
  attachments; PNGs display inline unless `?download=1`.
- Lifespan: load config, open the KB, start the curator thread, clean exports hourly. If connecting to
  BigQuery fails, keep running and explain the problem in chat (no crash loop).

## 6. Answer layout

```markdown
<1–2 sentence answer / takeaway>

| col | col |
|---|---|
| … | … |

[⬇️ Download CSV (12 rows)](/downloads/<id>.csv)

![<chart title>](/downloads/<id>.png)
[⬇️ Download chart](/downloads/<id>.png?download=1)

_Assumed: last 30 days; paid orders only._

<details><summary>Show SQL</summary>

```sql
SELECT …
```

</details>

_Want a chart of this? Just ask._
```

## 7. Code style ("humanly developed")

- Write code the way a careful person would: plain names, small functions, straightforward control flow.
- No speculative abstractions, factories, plugin frameworks or settings nobody asked for.
- Type hints on public functions. Docstrings say *why* or *what for*, not a restatement of the code.
- Comments only where the code isn't obvious. No banner comments, no emoji in code.
- Use only the dependencies in `pyproject.toml`. Ask before adding one.
- Raise clear exceptions. Never swallow an error silently: log it with context.
- Keep files under ~300 lines and split by responsibility.
- Tests: pytest, plain functions, fakes over heavy mocking, **no network, no real BigQuery, no real LLM**.
- `.venv/bin/ruff check .` and `.venv/bin/ruff format .` must be clean (line length 100).

## 8. Not in v1

MySQL connector, cross-database joins (DuckDB can attach both), embeddings, per-user access rules, PII
masking, audit log, schema refresh without restart, Cloud Run deployment, interactive charts, Excel export.
