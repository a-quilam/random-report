"""Persistence layer: SQLite schema, migrations and typed repositories.

The store is the only place that talks to SQLite. It keeps private facts
(transactions, occurrences, raw posts) inside the private database and stores
the public projection as an already leak-checked JSON snapshot produced by
``randreport.projection``. Nothing here invents money or events; it records what
the read-only connectors returned and what the domain computed.
"""

from .db import connect, transaction
from .migrations import (
    MIGRATIONS,
    REQUIRED_TABLES,
    apply_migrations,
    missing_tables,
    schema_version,
)

__all__ = [
    "connect",
    "transaction",
    "MIGRATIONS",
    "REQUIRED_TABLES",
    "apply_migrations",
    "missing_tables",
    "schema_version",
]
