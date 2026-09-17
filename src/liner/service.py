from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from .config import Settings
from .db import Database, decode_track
from .media import (
    SUPPORTED_EXTENSIONS,
    TAG_FIELDS,
    MediaError,
    ensure_safe_root,
    filename_suggestion,
    inspect_media,
    render_template,
    safe_library_path,
    sanitize_filename,
    unique_destination,
    write_artwork_copy,
    write_metadata_copy,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LibraryService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self._mutation_lock = threading.RLock()

    def scan(self) -> dict[str, int]:
        root = ensure_safe_root(self.settings.library_root)
        discovered: set[str] = set()
        indexed = 0
        failed = 0
        existing = {
            row["relative_path"]: row
            for row in self.database.fetch_all(
                "SELECT id, relative_path, size, mtime_ns FROM tracks WHERE status = 'active'"
            )
        }
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if path.is_symlink() or any(
                parent.is_symlink() for parent in path.parents if parent != root
            ):
                failed += 1
                continue
            relative = path.relative_to(root).as_posix()
            discovered.add(relative)
            stat = path.stat()
            old = existing.get(relative)
            if old and old["size"] == stat.st_size and old["mtime_ns"] == stat.st_mtime_ns:
                indexed += 1
                continue
            try:
                details = inspect_media(path)
            except MediaError:
                failed += 1
                continue
            track_id = old["id"] if old else str(uuid4())
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO tracks (
                        id, relative_path, status, size, mtime_ns, duration, bitrate,
                        format, tags_json, issues_json, has_artwork, quarantine_path, indexed_at
                    ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        relative_path=excluded.relative_path, status='active', size=excluded.size,
                        mtime_ns=excluded.mtime_ns, duration=excluded.duration,
                        bitrate=excluded.bitrate, format=excluded.format,
                        tags_json=excluded.tags_json, issues_json=excluded.issues_json,
                        has_artwork=excluded.has_artwork, quarantine_path=NULL,
                        indexed_at=excluded.indexed_at
                    """,
                    (
                        track_id,
                        relative,
                        details["size"],
                        details["mtime_ns"],
                        details["duration"],
                        details["bitrate"],
                        details["format"],
                        json.dumps(details["tags"]),
                        json.dumps(details["issues"]),
                        int(details["has_artwork"]),
                        utc_now(),
                    ),
                )
            indexed += 1
        missing = set(existing) - discovered
        if missing:
            with self.database.transaction() as connection:
                connection.executemany(
                    "DELETE FROM tracks WHERE relative_path = ? AND status = 'active'",
                    [(relative,) for relative in missing],
                )
        return {"indexed": indexed, "failed": failed, "missing_removed": len(missing)}

    def list_tracks(
        self,
        *,
        search: str = "",
        status: str = "active",
        issue: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["status = ?"]
        params: list[Any] = [status]
        if search:
            clauses.append("(relative_path LIKE ? OR tags_json LIKE ?)")
            needle = f"%{search[:100]}%"
            params.extend([needle, needle])
        if issue:
            clauses.append("issues_json LIKE ?")
            params.append(f'%"{issue[:40]}"%')
        params.extend([min(max(limit, 1), 500), max(offset, 0)])
        rows = self.database.fetch_all(
            f"SELECT * FROM tracks WHERE {' AND '.join(clauses)} "
            "ORDER BY relative_path COLLATE NOCASE LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [self._decorate(decode_track(row)) for row in rows]

    def stats(self) -> dict[str, Any]:
        rows = self.database.fetch_all(
            "SELECT status, issues_json, COUNT(*) AS count FROM tracks GROUP BY status, issues_json"
        )
        result: dict[str, Any] = {
            "total": 0,
            "active": 0,
            "quarantined": 0,
            "needs_attention": 0,
            "issues": {},
        }
        for row in rows:
            count = int(row["count"])
            result["total"] += count
            if row["status"] == "active":
                result["active"] += count
                issues = json.loads(row["issues_json"])
                if issues:
                    result["needs_attention"] += count
                for issue in issues:
                    result["issues"][issue] = result["issues"].get(issue, 0) + count
            elif row["status"] == "quarantined":
                result["quarantined"] += count
        return result

    def get_track(self, track_id: str) -> dict[str, Any]:
        row = self.database.fetch_one("SELECT * FROM tracks WHERE id = ?", (track_id,))
        if not row:
            raise KeyError("track not found")
        return self._decorate(decode_track(row))

    def _decorate(self, track: dict[str, Any]) -> dict[str, Any]:
        track["filename"] = Path(track["relative_path"]).name
        track["suggested"] = filename_suggestion(Path(track["relative_path"]))
        return track

    def resolve_active(self, track_id: str) -> tuple[dict[str, Any], Path]:
        track = self.get_track(track_id)
        if track["status"] != "active":
            raise MediaError("Track is not in the active library")
        return track, safe_library_path(self.settings.library_root, track["relative_path"])

    def preview_name(self, track_id: str, template: str) -> str:
        track = self.get_track(track_id)
        return render_template(template, track["tags"], f".{track['format']}")

    def update_track(
        self,
        track_id: str,
        tags: dict[str, str],
        filename: str | None = None,
    ) -> dict[str, Any]:
        clean_tags = {field: str(tags.get(field, ""))[:500].strip() for field in TAG_FIELDS}
        with self._mutation_lock:
            track, source = self.resolve_active(track_id)
            operation_id = str(uuid4())
            backup = self.settings.backup_dir / f"{operation_id}{source.suffix.lower()}"
            shutil.copy2(source, backup)
            temporary = source.with_name(
                f".{source.stem}.{operation_id}.liner-tmp{source.suffix.lower()}"
            )
            destination = source
            if filename:
                safe_name = sanitize_filename(filename, source.suffix)
                destination = source.with_name(safe_name)
                safe_library_path(
                    self.settings.library_root,
                    destination.relative_to(self.settings.library_root).as_posix(),
                    must_exist=False,
                )
                if destination != source and destination.exists():
                    backup.unlink(missing_ok=True)
                    raise FileExistsError("A file with that name already exists")
            before = {"relative_path": track["relative_path"], "tags": track["tags"]}
            try:
                write_metadata_copy(source, temporary, clean_tags)
                os.replace(temporary, source)
                if destination != source:
                    os.replace(source, destination)
                details = inspect_media(destination)
                relative = destination.relative_to(self.settings.library_root).as_posix()
                after = {"relative_path": relative, "tags": details["tags"]}
                self._record_update(
                    track_id, relative, details, operation_id, "edit", before, after, backup
                )
            except Exception:
                temporary.unlink(missing_ok=True)
                if destination != source:
                    destination.unlink(missing_ok=True)
                shutil.copy2(backup, source)
                backup.unlink(missing_ok=True)
                raise
            return self.get_track(track_id)

    def update_artwork(self, track_id: str, image: bytes, mime: str) -> dict[str, Any]:
        with self._mutation_lock:
            track, source = self.resolve_active(track_id)
            operation_id = str(uuid4())
            backup = self.settings.backup_dir / f"{operation_id}{source.suffix.lower()}"
            temporary = source.with_name(
                f".{source.stem}.{operation_id}.liner-tmp{source.suffix.lower()}"
            )
            shutil.copy2(source, backup)
            try:
                write_artwork_copy(source, temporary, image, mime)
                os.replace(temporary, source)
                details = inspect_media(source)
                before = {
                    "relative_path": track["relative_path"],
                    "has_artwork": track["has_artwork"],
                }
                after = {"relative_path": track["relative_path"], "has_artwork": True}
                self._record_update(
                    track_id,
                    track["relative_path"],
                    details,
                    operation_id,
                    "artwork",
                    before,
                    after,
                    backup,
                )
            except Exception:
                temporary.unlink(missing_ok=True)
                shutil.copy2(backup, source)
                backup.unlink(missing_ok=True)
                raise
            return self.get_track(track_id)

    def _record_update(
        self,
        track_id: str,
        relative: str,
        details: dict[str, Any],
        operation_id: str,
        kind: str,
        before: dict[str, Any],
        after: dict[str, Any],
        backup: Path,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE tracks SET relative_path=?, size=?, mtime_ns=?, duration=?, bitrate=?,
                    format=?, tags_json=?, issues_json=?, has_artwork=?, indexed_at=? WHERE id=?
                """,
                (
                    relative,
                    details["size"],
                    details["mtime_ns"],
                    details["duration"],
                    details["bitrate"],
                    details["format"],
                    json.dumps(details["tags"]),
                    json.dumps(details["issues"]),
                    int(details["has_artwork"]),
                    utc_now(),
                    track_id,
                ),
            )
            connection.execute(
                "INSERT INTO operations VALUES (?, ?, 'applied', ?, ?, ?, ?, ?, NULL)",
                (
                    operation_id,
                    kind,
                    track_id,
                    json.dumps(before),
                    json.dumps(after),
                    str(backup),
                    utc_now(),
                ),
            )

    def quarantine(self, track_id: str) -> dict[str, Any]:
        with self._mutation_lock:
            track, source = self.resolve_active(track_id)
            operation_id = str(uuid4())
            destination = (
                self.settings.quarantine_dir / f"{operation_id}{source.suffix.lower()}"
            )
            os.replace(source, destination)
            try:
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE tracks SET status='quarantined', quarantine_path=? WHERE id=?",
                        (str(destination), track_id),
                    )
                    connection.execute(
                        "INSERT INTO operations VALUES (?, 'quarantine', 'applied', ?, ?, ?, ?, ?, NULL)",
                        (
                            operation_id,
                            track_id,
                            json.dumps({"relative_path": track["relative_path"]}),
                            json.dumps({"quarantine_path": str(destination)}),
                            str(destination),
                            utc_now(),
                        ),
                    )
            except Exception:
                os.replace(destination, source)
                raise
            return self.get_track(track_id)

    def restore(self, track_id: str) -> dict[str, Any]:
        with self._mutation_lock:
            track = self.get_track(track_id)
            if track["status"] != "quarantined" or not track["quarantine_path"]:
                raise MediaError("Track is not quarantined")
            source = Path(track["quarantine_path"])
            if not source.is_file() or not source.resolve().is_relative_to(
                self.settings.quarantine_dir.resolve()
            ):
                raise MediaError("Quarantined file is unavailable")
            destination = safe_library_path(
                self.settings.library_root, track["relative_path"], must_exist=False
            )
            if destination.exists():
                raise FileExistsError("The original library path is occupied")
            destination.parent.mkdir(parents=True, exist_ok=True)
            details = inspect_media(source)
            os.replace(source, destination)
            operation_id = str(uuid4())
            try:
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE tracks SET status='active', quarantine_path=NULL, size=?, mtime_ns=?,
                            tags_json=?, issues_json=?, has_artwork=?, indexed_at=? WHERE id=?
                        """,
                        (
                            details["size"],
                            details["mtime_ns"],
                            json.dumps(details["tags"]),
                            json.dumps(details["issues"]),
                            int(details["has_artwork"]),
                            utc_now(),
                            track_id,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO operations VALUES (?, 'restore', 'applied', ?, ?, ?, NULL, ?, NULL)",
                        (
                            operation_id,
                            track_id,
                            json.dumps({"quarantine_path": str(source)}),
                            json.dumps({"relative_path": track["relative_path"]}),
                            utc_now(),
                        ),
                    )
            except Exception:
                os.replace(destination, source)
                raise
            return self.get_track(track_id)

    def purge(self, track_id: str) -> None:
        with self._mutation_lock:
            track = self.get_track(track_id)
            if track["status"] != "quarantined" or not track["quarantine_path"]:
                raise MediaError("Only quarantined tracks can be permanently deleted")
            path = Path(track["quarantine_path"])
            if not path.resolve().is_relative_to(self.settings.quarantine_dir.resolve()):
                raise MediaError("Invalid quarantine path")
            tombstone = self.settings.staging_dir / f"purge-{uuid4()}{path.suffix.lower()}"
            os.replace(path, tombstone)
            operation_id = str(uuid4())
            try:
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE tracks SET status='deleted', quarantine_path=NULL WHERE id=?",
                        (track_id,),
                    )
                    connection.execute(
                        "INSERT INTO operations VALUES (?, 'purge', 'applied', ?, ?, '{}', NULL, ?, NULL)",
                        (
                            operation_id,
                            track_id,
                            json.dumps({"relative_path": track["relative_path"]}),
                            utc_now(),
                        ),
                    )
            except Exception:
                os.replace(tombstone, path)
                raise
            tombstone.unlink(missing_ok=True)

    def add_stream(self, stream: BinaryIO, original_name: str) -> dict[str, Any]:
        extension = Path(original_name).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise MediaError("Unsupported audio extension")
        operation_id = str(uuid4())
        staging = self.settings.staging_dir / f"{operation_id}{extension}"
        total = 0
        try:
            with staging.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.settings.max_upload_bytes:
                        raise MediaError("Upload exceeds the configured size limit")
                    output.write(chunk)
            inspect_media(staging)
            filename = sanitize_filename(original_name, extension)
            destination = unique_destination(
                ensure_safe_root(self.settings.library_root), filename
            )
            os.replace(staging, destination)
            try:
                self.scan()
                relative = destination.relative_to(self.settings.library_root).as_posix()
                row = self.database.fetch_one(
                    "SELECT * FROM tracks WHERE relative_path=? AND status='active'",
                    (relative,),
                )
                if not row:
                    raise RuntimeError("Imported track was not indexed")
                track = decode_track(row)
                with self.database.transaction() as connection:
                    connection.execute(
                        "INSERT INTO operations VALUES (?, 'add', 'applied', ?, '{}', ?, NULL, ?, NULL)",
                        (
                            operation_id,
                            track["id"],
                            json.dumps({"relative_path": relative}),
                            utc_now(),
                        ),
                    )
            except Exception:
                destination.unlink(missing_ok=True)
                with self.database.transaction() as connection:
                    connection.execute(
                        "DELETE FROM tracks WHERE relative_path=? AND status='active'",
                        (destination.relative_to(self.settings.library_root).as_posix(),),
                    )
                raise
            return self._decorate(track)
        except Exception:
            staging.unlink(missing_ok=True)
            raise

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT id, kind, status, track_id, before_json, after_json, created_at, reversed_at "
            "FROM operations ORDER BY created_at DESC LIMIT ?",
            (min(max(limit, 1), 200),),
        )
        for row in rows:
            row["before"] = json.loads(row.pop("before_json"))
            row["after"] = json.loads(row.pop("after_json"))
        return rows

    def undo(self, operation_id: str) -> dict[str, Any]:
        with self._mutation_lock:
            operation = self.database.fetch_one(
                "SELECT * FROM operations WHERE id=?", (operation_id,)
            )
            if not operation or operation["status"] != "applied":
                raise MediaError("Operation cannot be undone")
            if operation["kind"] == "quarantine":
                result = self.restore(operation["track_id"])
            elif operation["kind"] in {"edit", "artwork"}:
                track = self.get_track(operation["track_id"])
                current = safe_library_path(self.settings.library_root, track["relative_path"])
                before = json.loads(operation["before_json"])
                destination = safe_library_path(
                    self.settings.library_root, before["relative_path"], must_exist=False
                )
                if destination != current and destination.exists():
                    raise FileExistsError("The original path is occupied")
                backup = Path(operation["backup_path"])
                if not backup.is_file() or not backup.resolve().is_relative_to(
                    self.settings.backup_dir.resolve()
                ):
                    raise MediaError("Backup is unavailable")
                undo_id = str(uuid4())
                undo_backup = self.settings.backup_dir / f"{undo_id}{current.suffix.lower()}"
                shutil.copy2(current, undo_backup)
                try:
                    if destination != current:
                        current.unlink()
                    shutil.copy2(backup, destination)
                    details = inspect_media(destination)
                    self._record_update(
                        track["id"],
                        before["relative_path"],
                        details,
                        undo_id,
                        "undo",
                        {"operation": operation_id},
                        before,
                        undo_backup,
                    )
                except Exception:
                    destination.unlink(missing_ok=True)
                    shutil.copy2(undo_backup, current)
                    undo_backup.unlink(missing_ok=True)
                    raise
                result = self.get_track(track["id"])
            else:
                raise MediaError(f"{operation['kind']} operations cannot be undone")
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE operations SET status='reversed', reversed_at=? WHERE id=?",
                    (utc_now(), operation_id),
                )
            return result
