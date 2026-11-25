from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from backend import llm as llm_module
from backend.config import LLMConfig
from backend.llm import LLMClient, LLMError, _extract_json


class TablePick(BaseModel):
    tables: list[str]


def reply(text: str):
    """Shape of a LiteLLM completion response, reduced to what the client reads."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


@pytest.fixture
def client():
    return LLMClient(LLMConfig(fast_model="openai/fast-model", smart_model="openai/smart-model"))


USER = [{"role": "user", "content": "Which tables hold orders?"}]


def test_tier_selects_the_model(client, monkeypatch):
    models = []

    def fake_completion(**kwargs):
        models.append(kwargs["model"])
        return reply("ok")

    monkeypatch.setattr(llm_module.litellm, "completion", fake_completion)

    client.complete(USER, tier="smart")
    client.complete(USER)

    assert models == ["openai/smart-model", "openai/fast-model"]


def test_json_inside_code_fence_is_parsed(client, monkeypatch):
    fenced = '```json\n{"tables": ["sales.orders"]}\n```'
    monkeypatch.setattr(llm_module.litellm, "completion", lambda **kwargs: reply(fenced))

    assert client.complete_json(USER, TablePick).tables == ["sales.orders"]


def test_invalid_json_gets_one_repair_attempt(client, monkeypatch):
    replies = iter(["sorry, here you go: tables are orders", '{"tables": []}'])
    sent = []

    def fake_completion(**kwargs):
        sent.append(kwargs["messages"])
        return reply(next(replies))

    monkeypatch.setattr(llm_module.litellm, "completion", fake_completion)

    assert client.complete_json(USER, TablePick).tables == []
    assert len(sent) == 2
    assert "not valid" in sent[1][-1]["content"]


def test_gives_up_after_the_repair_attempt(client, monkeypatch):
    monkeypatch.setattr(llm_module.litellm, "completion", lambda **kwargs: reply("still no json"))

    with pytest.raises(LLMError, match="TablePick"):
        client.complete_json(USER, TablePick)


def test_json_rules_are_merged_into_the_first_system_message(client, monkeypatch):
    sent = {}

    def fake_completion(**kwargs):
        sent["messages"] = kwargs["messages"]
        return reply('{"tables": []}')

    monkeypatch.setattr(llm_module.litellm, "completion", fake_completion)

    client.complete_json([{"role": "system", "content": "You pick tables."}, *USER], TablePick)

    messages = sent["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"].startswith("You pick tables.")
    assert "JSON schema" in messages[0]["content"]


def test_provider_errors_become_llm_errors(client, monkeypatch):
    def failing_completion(**kwargs):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(llm_module.litellm, "completion", failing_completion)

    with pytest.raises(LLMError, match="rate limited"):
        client.complete(USER)


def test_extract_json_ignores_surrounding_prose():
    assert _extract_json('Sure! {"a": 1} Hope that helps.') == '{"a": 1}'
