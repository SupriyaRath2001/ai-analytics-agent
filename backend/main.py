"""The HTTP API: an OpenAI-compatible chat endpoint for Open WebUI, plus health and downloads.

Open WebUI sees one model, ``analytics-agent``. Each chat request goes to AnalyticsAgent, and its
markdown chunks go back as OpenAI ``chat.completion.chunk`` events, or as one completion when the
client doesn't stream.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from backend.agent.router import AnalyticsAgent
from backend.bootstrap.curator import Curator
from backend.bootstrap.kb_cache import KBCache
from backend.config import AppConfig, load_config
from backend.connectors.base import BaseDBConnector
from backend.connectors.registry import create_connector
from backend.llm import LLMClient
from backend.services.exports import ExportStore

log = logging.getLogger(__name__)

MODEL_ID = "analytics-agent"
EXPORT_CLEANUP_INTERVAL_SEC = 3600
_STARTED_AT = int(time.time())  # the "created" time OpenAI's model list asks for


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build everything from the config, start the background threads, clean up on shutdown.

    A connection that fails to connect is logged and left out. The API still starts and the chat
    explains that BigQuery isn't connected, instead of the container restarting in a loop.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_config()
    kb = KBCache(config.kb_path)
    llm = LLMClient(config.llm)
    output = config.output
    store = ExportStore(output.export_dir, output.public_base_url, output.export_ttl_hours)
    store.cleanup()
    connectors = _connect(config)

    curator = Curator(connectors, kb, llm, config.business_context_path)
    threading.Thread(target=curator.run, name="curator", daemon=True).start()
    stop_cleanup = threading.Event()
    threading.Thread(
        target=_clean_exports, args=(store, stop_cleanup), name="export-cleanup", daemon=True
    ).start()

    app.state.config = config
    app.state.kb = kb
    app.state.store = store
    app.state.curator = curator
    app.state.agent = AnalyticsAgent(config, connectors, kb, llm, store)
    yield

    stop_cleanup.set()
    for name, connector in connectors.items():
        try:
            connector.close()
        except Exception:
            log.exception("could not close the connection %r", name)
    kb.close()


def _connect(config: AppConfig) -> dict[str, BaseDBConnector]:
    """Connect each enabled connection. One that fails is logged and skipped."""
    connectors: dict[str, BaseDBConnector] = {}
    for connection in config.enabled_connections:
        if connection.name in connectors:
            log.error(
                "skipping a second connection named %r: names must be unique", connection.name
            )
            continue
        try:
            connector = create_connector(connection)
            connector.connect()
        except Exception:
            log.exception("could not connect to %r; the agent runs without it", connection.name)
            continue
        connectors[connection.name] = connector
    if not connectors:
        log.error("no database is connected; the chat will say so until this is fixed")
    return connectors


def _clean_exports(store: ExportStore, stop: threading.Event) -> None:
    """Delete expired downloads every hour until ``stop`` is set."""
    while not stop.wait(EXPORT_CLEANUP_INTERVAL_SEC):
        try:
            store.cleanup()
        except Exception:
            log.exception("could not clean up expired export files")


def require_api_key(request: Request) -> None:
    """Bearer auth for /v1/* when agent_api_key is set. Open WebUI sends it as its OpenAI key."""
    expected = request.app.state.config.agent_api_key
    if not expected:
        return
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token.strip().encode(), expected.encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )


public = APIRouter()
v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


@public.get("/health")
def health(request: Request) -> dict:
    state = request.app.state
    return {"status": "ok", "curation": state.curator.status, "kb": state.kb.stats()}


@public.get("/downloads/{file_name}")
def get_download(file_name: str, request: Request, download: bool = False) -> FileResponse:
    """A CSV or chart PNG from the export store. CSVs download; PNGs show unless ?download=1."""
    path = request.app.state.store.path_for(file_name)
    if path is None:
        raise HTTPException(status_code=404, detail="This file doesn't exist or has expired.")
    if path.suffix == ".csv":
        return FileResponse(path, media_type="text/csv", filename="query-result.csv")
    return FileResponse(
        path,
        media_type="image/png",
        filename="chart.png",
        content_disposition_type="attachment" if download else "inline",
    )


@v1.get("/models")
def list_models() -> dict:
    model = {
        "id": MODEL_ID,
        "object": "model",
        "created": _STARTED_AT,
        "owned_by": "ai-analytics-agent",
    }
    return {"object": "list", "data": [model]}


@v1.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    messages, stream = await _read_chat_request(request)
    pieces = request.app.state.agent.run(messages)
    if stream:
        # Starlette iterates a plain generator in its thread pool, so the agent's blocking
        # calls (LLM, BigQuery) don't hold up other requests.
        return StreamingResponse(_sse_events(pieces), media_type="text/event-stream")
    text = await run_in_threadpool("".join, pieces)
    return JSONResponse(_completion(text))


async def _read_chat_request(request: Request) -> tuple[list[dict], bool]:
    """``messages`` and ``stream`` from an OpenAI-style body. Other fields are ignored."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="The request body must be JSON.") from None
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        raise HTTPException(status_code=400, detail="'messages' must be a list of chat messages.")
    return messages, body.get("stream") is True


def _sse_events(pieces: Iterator[str]) -> Iterator[str]:
    """The agent's markdown pieces as server-sent ``chat.completion.chunk`` events."""
    completion_id, created = _completion_id(), int(time.time())

    def event(delta: dict, finish_reason: str | None = None) -> str:
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    yield event({"role": "assistant", "content": ""})
    for piece in pieces:
        if piece:
            yield event({"content": piece})
    yield event({}, finish_reason="stop")
    yield "data: [DONE]\n\n"


def _completion(text: str) -> dict:
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def create_app() -> FastAPI:
    """Build the app. Uvicorn serves ``app`` below, whose lifespan reads the real config.

    Tests call create_app(), put fakes on ``app.state`` (agent, curator, kb, store and config)
    and use TestClient without a ``with`` block. TestClient only runs the lifespan inside a
    ``with`` block, so no config file, BigQuery or LLM is needed.
    """
    app = FastAPI(title="AI Analytics Agent", lifespan=lifespan)
    app.include_router(public)
    app.include_router(v1)
    return app


app = create_app()
