"""Load ``config/config.yaml``, fill in ``${VAR}`` values from the environment, validate it."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

DEFAULT_CONFIG_PATH = "config/config.yaml"

# ${NAME} or ${NAME:-default}
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


# An optional string where "" (e.g. an empty ${VAR:-}) means "not set".
OptionalStr = Annotated[str | None, BeforeValidator(_blank_to_none)]


class ConnectionConfig(BaseModel):
    """One database connection. Extra keys are kept for connector types that need them."""

    model_config = ConfigDict(extra="allow")

    name: str
    type: str
    enabled: bool = True
    project: OptionalStr = None
    location: OptionalStr = None
    credentials_path: OptionalStr = None
    include_datasets: list[str] = Field(default_factory=list)
    exclude_datasets: list[str] = Field(default_factory=list)
    exclude_tables: list[str] = Field(default_factory=list)
    max_bytes_billed_gb: float | None = None
    query_timeout_sec: int = 120
    max_rows_fetch: int = 50_000


class LLMConfig(BaseModel):
    fast_model: str
    smart_model: str
    api_key: OptionalStr = None
    api_base: OptionalStr = None
    temperature: float = 0.0
    timeout_sec: int = 120
    num_retries: int = 2


class OutputConfig(BaseModel):
    inline_table_max_rows: int = 20
    inline_table_max_columns: int = 12
    preview_rows: int = 5
    export_dir: str = "exports"
    export_ttl_hours: int = 24
    public_base_url: str = ""

    @field_validator("public_base_url", mode="before")
    @classmethod
    def _strip_slash(cls, value: Any) -> str:
        return (value or "").rstrip("/")


class AppConfig(BaseModel):
    connections: list[ConnectionConfig]
    llm: LLMConfig
    output: OutputConfig = Field(default_factory=OutputConfig)
    timezone: str = "UTC"
    kb_path: str = "data/kb_cache.duckdb"
    business_context_path: OptionalStr = "config/business_context.md"
    agent_api_key: OptionalStr = None

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}, use a name like 'Asia/Kolkata'") from exc
        return value

    @property
    def enabled_connections(self) -> list[ConnectionConfig]:
        return [c for c in self.connections if c.enabled]


def _expand(text: str) -> str:
    """Replace ${NAME} / ${NAME:-default} with environment values, like a shell does."""

    def replace(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        value = os.environ.get(name, "")
        if value:
            return value
        if default is not None:
            return default
        if name in os.environ:
            return ""
        raise ConfigError(f"environment variable {name} is not set (it is used in the config file)")

    return _ENV_REF.sub(replace, text)


def _expand_all(value: Any) -> Any:
    if isinstance(value, str):
        return _expand(value)
    if isinstance(value, list):
        return [_expand_all(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_all(item) for key, item in value.items()}
    return value


def load_config(path: str | Path | None = None) -> AppConfig:
    """Read the YAML config. ``CONFIG_PATH`` overrides the default location."""
    load_dotenv()  # picks up .env if present; real environment variables win
    path = Path(path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}. Copy config/config.yaml.example to {path} first."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        return AppConfig.model_validate(_expand_all(raw))
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}:\n{exc}") from exc
