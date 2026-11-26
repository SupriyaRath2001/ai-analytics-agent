import pytest

from backend.config import ConnectionConfig
from backend.connectors import registry
from backend.connectors.base import BaseDBConnector
from backend.connectors.bigquery_connector import BigQueryConnector
from backend.connectors.registry import CONNECTOR_TYPES, create_connector


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


def test_bigquery_is_registered():
    assert CONNECTOR_TYPES["bigquery"] is BigQueryConnector


def test_create_connector_does_not_connect_yet():
    connector = create_connector(ConnectionConfig(name="warehouse", type="bigquery"))

    assert isinstance(connector, BigQueryConnector)
    assert connector.name == "warehouse"
    assert connector.client is None


def test_a_new_type_needs_only_its_line_in_connector_types(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "fake", FakeConnector)

    connector = create_connector(ConnectionConfig(name="shop", type="fake"))

    assert isinstance(connector, FakeConnector)
    assert connector.connected is False  # main.py connects each one itself


def test_unknown_type_names_the_known_types():
    with pytest.raises(ValueError, match="'mysql'.*known types: bigquery"):
        create_connector(ConnectionConfig(name="shop", type="mysql"))


def test_connectors_are_built_only_through_create_connector():
    # build_connectors was a second, unused way to build them; main.py builds them one by one.
    assert not hasattr(registry, "build_connectors")
