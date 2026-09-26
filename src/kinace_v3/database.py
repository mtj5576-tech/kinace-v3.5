from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import database_path, validate_database_path


SCHEMA_VERSION = "3"


def initialize_database(path: str | Path | None = None) -> Path:
    target = database_path() if path is None else validate_database_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(target)) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_metadata (key, value)
            VALUES ('schema_version', '3')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """
        )
    return target
