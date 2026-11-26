"""Maps the ``type`` of a connection in config.yaml to its connector class.

A new database is one connector file plus one line in ``CONNECTOR_TYPES``.
"""

from __future__ import annotations

from backend.config import ConnectionConfig
from backend.connectors.base import BaseDBConnector
from backend.connectors.bigquery_connector import BigQueryConnector

CONNECTOR_TYPES: dict[str, type[BaseDBConnector]] = {
    BigQueryConnector.type_name: BigQueryConnector,
}


def create_connector(config: ConnectionConfig) -> BaseDBConnector:
    """Build the connector for one connection, without connecting yet."""
    connector_class = CONNECTOR_TYPES.get(config.type)
    if connector_class is None:
        known = ", ".join(sorted(CONNECTOR_TYPES))
        raise ValueError(
            f"connection {config.name!r} has unknown type {config.type!r}; known types: {known}"
        )
    return connector_class(config)
