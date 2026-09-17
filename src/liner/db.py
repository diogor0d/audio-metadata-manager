from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    duration REAL,
    bitrate INTEGER,
    format TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    issues_json TEXT NOT NULL,
    has_artwork INTEGER NOT NULL DEFAULT 0,
    quarantine_path TEXT,
    indexed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_active_path
ON tracks(relative_path) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    track_id TEXT,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    backup_path TEXT,
    created_at TEXT NOT NULL,
    reversed_at TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS downloads (
    id TEXT PRIMARY KEY,
    remote_id TEXT NOT NULL,
    status TEXT NOT NULL,
    poll_path TEXT NOT NULL,
    file_path TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            table = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='tracks'"
            ).fetchone()
            if table and "RELATIVE_PATH TEXT NOT NULL UNIQUE" in table["sql"].upper():
                self._migrate_track_path_uniqueness(connection)

    @staticmethod
    def _migrate_track_path_uniqueness(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            ALTER TABLE tracks RENAME TO tracks_legacy;
            DROP INDEX IF EXISTS idx_tracks_status;
            DROP INDEX IF EXISTS idx_tracks_active_path;
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                duration REAL,
                bitrate INTEGER,
                format TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                issues_json TEXT NOT NULL,
                has_artwork INTEGER NOT NULL DEFAULT 0,
                quarantine_path TEXT,
                indexed_at TEXT NOT NULL
            );
            INSERT INTO tracks SELECT * FROM tracks_legacy;
            DROP TABLE tracks_legacy;
            CREATE INDEX idx_tracks_status ON tracks(status);
            CREATE UNIQUE INDEX idx_tracks_active_path
                ON tracks(relative_path) WHERE status = 'active';
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
            return dict(row) if row else None


def decode_track(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["tags"] = json.loads(decoded.pop("tags_json"))
    decoded["issues"] = json.loads(decoded.pop("issues_json"))
    decoded["has_artwork"] = bool(decoded["has_artwork"])
    return decoded
