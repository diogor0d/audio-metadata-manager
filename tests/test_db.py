from __future__ import annotations

import sqlite3
from pathlib import Path

from liner.db import Database


def test_migrates_legacy_global_path_uniqueness(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
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
            INSERT INTO tracks VALUES (
                'old', 'same.mp3', 'quarantined', 1, 1, 1, 1,
                'mp3', '{}', '[]', 0, 'quarantine-file', 'now'
            );
            """
        )

    database = Database(path)
    database.initialize()

    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO tracks VALUES (
                'new', 'same.mp3', 'active', 1, 1, 1, 1,
                'mp3', '{}', '[]', 0, NULL, 'now'
            )
            """
        )
    assert len(database.fetch_all("SELECT id FROM tracks WHERE relative_path='same.mp3'")) == 2
