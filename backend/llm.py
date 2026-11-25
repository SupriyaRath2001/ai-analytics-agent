"""A thin wrapper around LiteLLM with two model tiers.

``fast`` is a cheap model for easy steps (routing, summaries, chart specs).
``smart`` is a stronger model for writing and fixing SQL.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Literal, TypeVar

import litellm
from pydantic import BaseModel, ValidationError

from backend.config import LLMConfig

log = logging.getLogger(__name__)

# Drop parameters a provider does not support (e.g. temperature on some models)
# instead of failing the whole call.
litellm.drop_params = True

Tier = Literal["fast", "smart"]
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The model call failed or never returned usable output."""


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    def model_for(self, tier: Tier) -> str:
        return self.config.smart_model if tier == "smart" else self.config.fast_model

    def complete(self, messages: list[dict], tier: Tier = "fast") -> str:
        """Send a chat and return the reply text."""
        model = self.model_for(tier)
        try:
            response = litellm.completion(
                model=model,
                messages=messages,
                temperature=self.config.temperature,
                api_key=self.config.api_key,
                api_base=self.config.api_base,
                timeout=self.config.timeout_sec,
                num_retries=self.config.num_retries,
            )
        except Exception as exc:  # LiteLLM raises many provider-specific types
            raise LLMError(f"LLM call to {model} failed: {exc}") from exc
        return response.choices[0].message.content or ""

    def complete_json(self, messages: list[dict], schema: type[T], tier: Tier = "fast") -> T:
        """Ask for a JSON object matching ``schema``. Retries once if the reply doesn't validate."""
        chat = _with_json_instructions(messages, schema)
        reply = self.complete(chat, tier)
        try:
            return _parse(reply, schema)
        except (ValueError, ValidationError) as first_error:
            log.info("reply for %s did not validate, asking once to fix it", schema.__name__)
            fix_request = (
                f"That reply was not valid: {str(first_error)[:800]}\n"
                "Reply again with only the corrected JSON object."
            )
            chat = [
                *chat,
                {"role": "assistant", "content": reply},
                {"role": "user", "content": fix_request},
            ]
            reply = self.complete(chat, tier)
            try:
                return _parse(reply, schema)
            except (ValueError, ValidationError) as exc:
                raise LLMError(
                    f"model did not return valid JSON for {schema.__name__}: {exc}"
                ) from exc


def _with_json_instructions(messages: list[dict], schema: type[BaseModel]) -> list[dict]:
    """Add the JSON rules to the first system message (some providers only accept one, first)."""
    rules = (
        "Reply with a single JSON object and nothing else (no prose, no code fences). "
        f"It must match this JSON schema:\n{json.dumps(schema.model_json_schema())}"
    )
    messages = [dict(message) for message in messages]
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = f"{messages[0]['content']}\n\n{rules}"
    else:
        messages.insert(0, {"role": "system", "content": rules})
    return messages


def _parse(reply: str, schema: type[T]) -> T:
    return schema.model_validate_json(_extract_json(reply))


def _extract_json(reply: str) -> str:
    """Pull the JSON object out of a reply that may be wrapped in ``` fences or prose."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", reply, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start, end = reply.find("{"), reply.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the reply")
    return reply[start : end + 1]
