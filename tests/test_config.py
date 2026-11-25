from pathlib import Path

import pytest

from backend.config import ConfigError, load_config

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config" / "config.yaml.example"

MINIMAL_CONFIG = """
connections:
  - name: bigquery
    type: bigquery
    project: ${GCP_PROJECT}
    credentials_path: ${GOOGLE_APPLICATION_CREDENTIALS:-}
llm:
  fast_model: ${FAST_MODEL:-openai/fast-model}
  smart_model: openai/smart-model
timezone: ${TZ:-UTC}
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ["GCP_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS", "FAST_MODEL", "TZ"]:
        monkeypatch.delenv(name, raising=False)


def test_env_values_and_defaults_are_filled_in(config_file, monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "my-project")

    config = load_config(config_file)

    connection = config.connections[0]
    assert connection.project == "my-project"
    assert connection.credentials_path is None  # an empty default means "not set"
    assert config.llm.fast_model == "openai/fast-model"
    assert config.timezone == "UTC"
    assert config.output.inline_table_max_rows == 20


def test_missing_env_var_names_the_variable(config_file):
    with pytest.raises(ConfigError, match="GCP_PROJECT"):
        load_config(config_file)


def test_unknown_timezone_is_rejected(config_file, monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "my-project")
    monkeypatch.setenv("TZ", "Mars/Olympus")

    with pytest.raises(ConfigError, match="timezone"):
        load_config(config_file)


def test_missing_file_explains_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="config.yaml.example"):
        load_config(tmp_path / "missing.yaml")


def test_example_config_is_valid(monkeypatch):
    for name in ["GCP_PROJECT", "FAST_MODEL", "SMART_MODEL"]:
        monkeypatch.setenv(name, "example")

    config = load_config(EXAMPLE_CONFIG)

    assert config.connections[0].type == "bigquery"
    assert config.connections[0].max_bytes_billed_gb == 50
    assert config.output.export_ttl_hours == 24
