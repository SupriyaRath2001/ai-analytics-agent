import inspect
import json
import logging
import time
from types import SimpleNamespace

import pandas as pd
import pytest
from anyio import to_thread
from fastapi.testclient import TestClient

from backend import main
from backend.bootstrap.curator import Curator
from backend.bootstrap.kb_cache import KBCache
from backend.config import AppConfig, LLMConfig
from backend.connectors import registry
from backend.connectors.base import BaseDBConnector
from backend.main import create_app
from backend.services.exports import ExportStore

URL = "/v1/chat/completions"
MESSAGES = [{"role": "user", "content": "Orders by status?"}]
PIECES = ["> Writing the SQL…\n", "\n", "Most orders are **paid**."]


class FakeAgent:
    def __init__(self):
        self.received = []

    def run(self, messages):
        self.received.append(messages)
        yield from PIECES


class FakeKB:
    def stats(self):
        return {"tables": 2, "curated": 1}


@pytest.fixture
def store(tmp_path):
    return ExportStore(tmp_path / "exports")


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
def make_client(store, agent):
    """A TestClient over fakes. Without a ``with`` block, the lifespan (real config) never runs."""

    def make(api_key=None):
        app = create_app()
        app.state.config = AppConfig(
            connections=[],
            llm=LLMConfig(fast_model="fake/fast", smart_model="fake/smart"),
            agent_api_key=api_key,
        )
        app.state.agent = agent
        app.state.curator = SimpleNamespace(status="done")
        app.state.kb = FakeKB()
        app.state.store = store
        return TestClient(app)

    return make


@pytest.fixture
def client(make_client):
    return make_client()


def sse_data(response):
    """The data of each server-sent event, in order."""
    return [block.removeprefix("data: ") for block in response.text.split("\n\n") if block]


def test_models_lists_the_one_agent_model(client):
    response = client.get("/v1/models")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [(model["id"], model["object"]) for model in body["data"]] == [
        ("analytics-agent", "model")
    ]


def test_completion_without_streaming(client, agent):
    body = {"model": "gpt-4o", "temperature": 0.3, "user": "x", "messages": MESSAGES}

    response = client.post(URL, json=body)

    assert response.status_code == 200
    completion = response.json()
    assert completion["object"] == "chat.completion"
    assert completion["model"] == "analytics-agent"
    assert completion["id"].startswith("chatcmpl-")
    assert completion["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "".join(PIECES)},
            "finish_reason": "stop",
        }
    ]
    assert completion["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert agent.received == [MESSAGES]


def test_streaming_sends_openai_chunks_then_done(client):
    response = client.post(URL, json={"stream": True, "messages": MESSAGES})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_data(response)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert {chunk["object"] for chunk in chunks} == {"chat.completion.chunk"}
    assert {chunk["model"] for chunk in chunks} == {"analytics-agent"}
    assert len({chunk["id"] for chunk in chunks}) == 1
    choices = [chunk["choices"][0] for chunk in chunks]
    assert choices[0] == {
        "index": 0,
        "delta": {"role": "assistant", "content": ""},
        "finish_reason": None,
    }
    assert [choice["delta"] for choice in choices[1:-1]] == [{"content": p} for p in PIECES]
    assert all(choice["finish_reason"] is None for choice in choices[:-1])
    assert choices[-1] == {"index": 0, "delta": {}, "finish_reason": "stop"}
    assert "".join(choice["delta"].get("content", "") for choice in choices) == "".join(PIECES)


@pytest.mark.parametrize(
    "raw",
    [b"{}", b'{"messages": "hello"}', b'{"messages": [1, 2]}', b"[]", b"{not json"],
)
def test_missing_or_invalid_messages_are_a_400(client, agent, raw):
    response = client.post(URL, content=raw, headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert agent.received == []


def test_api_key_is_required_on_v1_when_set(make_client, store):
    client = make_client(api_key="s3cret")
    csv_file = store.save_csv(pd.DataFrame({"a": [1]}))

    assert client.get("/v1/models").status_code == 401
    wrong = {"Authorization": "Bearer wrong"}
    assert client.get("/v1/models", headers=wrong).status_code == 401
    assert client.post(URL, json={"messages": MESSAGES}, headers=wrong).status_code == 401
    right = {"Authorization": "Bearer s3cret"}
    assert client.get("/v1/models", headers=right).status_code == 200
    assert client.post(URL, json={"messages": MESSAGES}, headers=right).status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get(f"/downloads/{csv_file}").status_code == 200


@pytest.mark.parametrize("name", ["notes.txt", "0" * 32 + ".csv", "..%2Fconfig.yaml"])
def test_downloads_unknown_or_malformed_names_are_404(client, name):
    assert client.get(f"/downloads/{name}").status_code == 404


def test_csv_download_is_an_attachment(client, store):
    csv_file = store.save_csv(pd.DataFrame({"city": ["Delhi"], "orders": [3]}))

    response = client.get(f"/downloads/{csv_file}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="query-result.csv"'
    assert response.content == "﻿city,orders\nDelhi,3\n".encode()


def test_png_shows_inline_unless_a_download_is_asked_for(client, store):
    png_file = store.save_png(b"\x89PNG fake chart")

    inline = client.get(f"/downloads/{png_file}")
    forced = client.get(f"/downloads/{png_file}?download=1")

    assert inline.headers["content-type"] == "image/png"
    assert inline.headers["content-disposition"] == 'inline; filename="chart.png"'
    assert inline.content == b"\x89PNG fake chart"
    assert forced.headers["content-disposition"] == 'attachment; filename="chart.png"'


def test_health_reports_curation_and_kb_counts(client):
    response = client.get("/health")

    assert response.json() == {
        "status": "ok",
        "curation": "done",
        "kb": {"tables": 2, "curated": 1},
    }


def test_health_and_models_never_wait_for_a_worker_thread():
    # A plain def endpoint or dependency runs in AnyIO's thread pool, where slow chat streams
    # can hold every thread. The Docker health check and Open WebUI must still get an answer.
    for function in (main.health, main.list_models, main.require_api_key):
        assert inspect.iscoroutinefunction(function), function.__name__


class StartupConnector(BaseDBConnector):
    """Connects, unless its connection is named "broken". "unreadable" can't list its tables."""

    type_name = "startup-fake"

    def __init__(self, config):
        super().__init__(config)
        self.closed = False

    @property
    def dialect(self):
        return "bigquery"

    def connect(self):
        if self.name == "broken":
            raise RuntimeError("403 Access Denied")

    def fetch_raw_metadata(self):
        if self.name == "unreadable":
            raise RuntimeError("403 Access Denied")
        return []

    def sample_column_values(self, table, columns, limit=15):
        return {}

    def dry_run_or_validate(self, query):
        return True, ""

    def execute_query(self, query, max_rows=None):
        raise NotImplementedError

    def close(self):
        self.closed = True


@pytest.fixture
def configured_app(tmp_path, monkeypatch):
    """make(*connection_names) -> an app whose lifespan runs on a temporary config.

    The real load_dotenv would copy a developer's .env into os.environ for the rest of the
    run. The fake sets AGENT_API_KEY instead, which the config needs, so the app only starts
    when .env is loaded before the config.
    """
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "startup-fake", StartupConnector)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setattr(
        main, "load_dotenv", lambda: monkeypatch.setenv("AGENT_API_KEY", "from-dotenv")
    )

    def make(*connection_names):
        config = {
            "connections": [{"name": name, "type": "startup-fake"} for name in connection_names],
            "llm": {"fast_model": "fake/fast", "smart_model": "fake/smart"},
            "kb_path": str(tmp_path / "kb.duckdb"),
            "business_context_path": None,
            "output": {"export_dir": str(tmp_path / "exports")},
            "agent_api_key": "${AGENT_API_KEY}",
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(json.dumps(config), encoding="utf-8")  # JSON is valid YAML
        monkeypatch.setenv("CONFIG_PATH", str(config_path))
        return create_app()

    return make


def wait_for_curation(curator):
    deadline = time.monotonic() + 10
    while curator.status not in ("done", "failed"):
        assert time.monotonic() < deadline, "the curator never finished"
        time.sleep(0.01)


def test_startup_keeps_running_when_a_connection_fails(configured_app, caplog):
    app = configured_app("broken", "warehouse")

    with caplog.at_level(logging.ERROR, logger="backend.main"), TestClient(app) as client:
        health = client.get("/health")
        connectors = app.state.agent.connectors

    assert health.status_code == 200
    assert health.json()["kb"] == {"tables": 0, "curated": 0}
    assert list(connectors) == ["warehouse"]
    assert connectors["warehouse"].closed is True
    assert "could not connect to 'broken'" in caplog.text


def test_startup_loads_dotenv_before_the_config(configured_app):
    app = configured_app("warehouse")

    with TestClient(app):
        config = app.state.config

    assert config.agent_api_key == "from-dotenv"


def test_startup_allows_100_worker_threads(configured_app):
    app = configured_app("warehouse")

    with TestClient(app) as client:
        # The limiter belongs to the event loop, so read it there.
        limit = client.portal.call(lambda: to_thread.current_default_thread_limiter().total_tokens)

    assert limit == 100


def test_the_agent_sees_that_the_catalog_could_not_be_read(configured_app):
    app = configured_app("unreadable")

    with TestClient(app) as client:
        wait_for_curation(app.state.curator)
        health = client.get("/health").json()
        status = app.state.agent.curation_status()

    assert health["curation"] == "failed"
    assert status == "failed"  # so the chat says "check the logs", not "try again in a minute"


def test_shutdown_stops_the_curator_before_closing_the_kb(configured_app, monkeypatch):
    events = []
    real_stop, real_close = Curator.stop, KBCache.close

    def stop(curator):
        events.append("curator stopped")
        real_stop(curator)

    def close(kb):
        events.append("kb closed")
        real_close(kb)

    monkeypatch.setattr(Curator, "stop", stop)
    monkeypatch.setattr(KBCache, "close", close)
    app = configured_app("warehouse")

    with TestClient(app):
        assert events == []

    assert events == ["curator stopped", "kb closed"]
