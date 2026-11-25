import pytest

from backend.config import ConnectionConfig
from backend.connectors import registry
from backend.connectors.base import BaseDBConnector
from backend.connectors.bigquery_connector import BigQueryConnector
from backend.connectors.registry import CONNECTOR_TYPES, build_connectors, create_connector


class FakeConnector(BaseDBConnector):
    type_name = "fake"

    def __init__(self, config):
        super().__init__(config)
        self.connected = False

    @property
    def dialect(self):
        return "duckdb"

    def connect(self):
        self.connected = True

    def fetch_raw_metadata(self):
        return []

    def sample_column_values(self, table, columns, limit=15):
        return {}

    def dry_run_or_validate(self, query):
        return True, ""

    def execute_query(self, query, max_rows=None):
        raise NotImplementedError


@pytest.fixture
def fake_type(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "fake", FakeConnector)


def test_bigquery_is_registered():
    assert CONNECTOR_TYPES["bigquery"] is BigQueryConnector


def test_create_connector_does_not_connect_yet():
    connector = create_connector(ConnectionConfig(name="warehouse", type="bigquery"))

    assert isinstance(connector, BigQueryConnector)
    assert connector.name == "warehouse"
    assert connector.client is None


def test_unknown_type_names_the_known_types():
    with pytest.raises(ValueError, match="'mysql'.*known types: bigquery"):
        create_connector(ConnectionConfig(name="shop", type="mysql"))


def test_build_connectors_connects_enabled_connections_only(fake_type):
    configs = [
        ConnectionConfig(name="main", type="fake"),
        ConnectionConfig(name="old", type="fake", enabled=False),
        ConnectionConfig(name="second", type="fake"),
    ]

    connectors = build_connectors(configs)

    assert list(connectors) == ["main", "second"]
    assert all(connector.connected for connector in connectors.values())


def test_duplicate_connection_names_are_rejected(fake_type):
    configs = [
        ConnectionConfig(name="main", type="fake"),
        ConnectionConfig(name="main", type="fake"),
    ]

    with pytest.raises(ValueError, match="'main'"):
        build_connectors(configs)
