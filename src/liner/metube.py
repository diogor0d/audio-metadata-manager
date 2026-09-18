from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .config import Settings
from .db import Database
from .service import LibraryService, utc_now


class MeTubeProtocolError(RuntimeError):
    pass


class MeTubeService:
    def __init__(self, settings: Settings, database: Database, library: LibraryService):
        self.settings = settings
        self.database = database
        self.library = library

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "Liner/1.0"}
        if self.settings.metube_cf_client_id:
            headers["CF-Access-Client-Id"] = self.settings.metube_cf_client_id
        if self.settings.metube_cf_client_secret:
            headers["CF-Access-Client-Secret"] = self.settings.metube_cf_client_secret
        if self.settings.metube_api_key:
            headers["X-Sidecar-Api-Key"] = self.settings.metube_api_key
        return headers

    async def create(
        self, source_url: str, *, target_track_id: str | None = None
    ) -> dict[str, Any]:
        if not self.settings.metube_enabled:
            raise RuntimeError("MeTube is not configured")
        parsed = urlparse(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            raise ValueError("A valid public HTTP or HTTPS media URL is required")
        if not parsed.hostname or parsed.hostname.lower() == "localhost":
            raise ValueError("Local and private media URLs are not allowed")
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)
            }
        except socket.gaierror as exc:
            raise ValueError("The media URL hostname could not be resolved") from exc
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("Local and private media URLs are not allowed")
        if target_track_id:
            track = self.library.get_track(target_track_id)
            if track["status"] != "active":
                raise ValueError("Only active tracks can be replaced")
            if not ({"missing_artist", "missing_title"} & set(track["issues"])):
                raise ValueError("Track replacement is available only for missing metadata")
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.post(
                f"{self.settings.metube_url}/jobs",
                headers=self._headers(),
                json={
                    "url": source_url,
                    "download_type": "audio",
                    "format": self.settings.metube_format,
                    "codec": self.settings.metube_format,
                    "quality": "best" if target_track_id else self.settings.metube_quality,
                },
            )
            response.raise_for_status()
            try:
                remote = response.json()
            except ValueError as exc:
                raise MeTubeProtocolError("MeTube returned an invalid response") from exc
        if not isinstance(remote, dict):
            raise MeTubeProtocolError("MeTube returned an invalid response")
        job_id = str(uuid4())
        now = utc_now()
        poll_path = str(remote.get("poll_url", ""))
        if not re.fullmatch(r"/jobs/[0-9a-f-]{36}", poll_path):
            raise MeTubeProtocolError("MeTube returned an unsafe job path")
        if not isinstance(remote.get("id"), str) or not isinstance(remote.get("status"), str):
            raise MeTubeProtocolError("MeTube returned an invalid job")
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO downloads (
                    id, remote_id, status, poll_path, file_path, error,
                    created_at, updated_at, target_track_id, purpose
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    remote["id"],
                    remote["status"],
                    poll_path,
                    now,
                    now,
                    target_track_id,
                    "replace" if target_track_id else "add",
                ),
            )
        return self.get_local(job_id)

    def get_local(self, job_id: str) -> dict[str, Any]:
        row = self.database.fetch_one("SELECT * FROM downloads WHERE id=?", (job_id,))
        if not row:
            raise KeyError("download job not found")
        return row

    async def refresh(self, job_id: str) -> dict[str, Any]:
        job = self.get_local(job_id)
        if job["status"] in {"imported", "importing"}:
            return job
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get(
                f"{self.settings.metube_url}{job['poll_path']}", headers=self._headers()
            )
            response.raise_for_status()
            try:
                remote = response.json()
            except ValueError as exc:
                raise MeTubeProtocolError("MeTube returned an invalid response") from exc
        if not isinstance(remote, dict) or not isinstance(remote.get("status"), str):
            raise MeTubeProtocolError("MeTube returned an invalid job")
        file_path = remote.get("file_url")
        error = remote.get("error")
        if file_path is not None and not isinstance(file_path, str):
            raise MeTubeProtocolError("MeTube returned an invalid file path")
        if error is not None and not isinstance(error, str):
            raise MeTubeProtocolError("MeTube returned an invalid job error")
        if file_path and not re.fullmatch(r"/jobs/[0-9a-f-]{36}/file", file_path):
            raise MeTubeProtocolError("MeTube returned an unsafe file path")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE downloads SET status=?, file_path=?, error=?, updated_at=? "
                "WHERE id=? AND status NOT IN ('importing', 'imported')",
                (remote["status"], file_path, error, utc_now(), job_id),
            )
        return self.get_local(job_id)

    async def import_completed(self, job_id: str) -> dict[str, Any]:
        local = self.get_local(job_id)
        if local["status"] == "imported":
            raise RuntimeError("Download has already been imported")
        if local["status"] == "importing":
            raise RuntimeError("Download is already being imported")
        job = await self.refresh(job_id)
        if job["status"] == "importing":
            raise RuntimeError("Download is already being imported")
        if job["status"] != "complete" or not job["file_path"]:
            raise RuntimeError("Download is not complete")
        with self.database.transaction() as connection:
            claimed = connection.execute(
                "UPDATE downloads SET status='importing', updated_at=? "
                "WHERE id=? AND status='complete'",
                (utc_now(), job_id),
            )
            if claimed.rowcount != 1:
                raise RuntimeError("Download is already being imported")
        temporary = self.settings.staging_dir / f"metube-{job_id}.download"
        total = 0
        try:
            async with (
                httpx.AsyncClient(timeout=120, follow_redirects=False) as client,
                client.stream(
                    "GET",
                    f"{self.settings.metube_url}{job['file_path']}",
                    headers=self._headers(),
                ) as response,
            ):
                response.raise_for_status()
                try:
                    declared = int(response.headers.get("content-length", "0") or 0)
                except ValueError as exc:
                    raise MeTubeProtocolError("MeTube returned an invalid file size") from exc
                if declared > self.settings.max_upload_bytes:
                    raise RuntimeError("Downloaded file exceeds the configured size limit")
                disposition = response.headers.get("content-disposition", "")
                match = re.search(r'filename="?([^";]+)', disposition)
                filename = match.group(1) if match else f"metube-{job_id}.mp3"
                with temporary.open("wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > self.settings.max_upload_bytes:
                            raise RuntimeError(
                                "Downloaded file exceeds the configured size limit"
                            )
                        output.write(chunk)

            def import_download() -> dict[str, Any]:
                with temporary.open("rb") as source:
                    if job["purpose"] == "replace" and job["target_track_id"]:
                        return self.library.replace_stream(
                            job["target_track_id"], source, filename
                        )
                    return self.library.add_stream(source, filename)

            result = await asyncio.to_thread(import_download)
        except Exception:
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE downloads SET status='complete', updated_at=? "
                    "WHERE id=? AND status='importing'",
                    (utc_now(), job_id),
                )
            raise
        finally:
            temporary.unlink(missing_ok=True)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE downloads SET status='imported', updated_at=? WHERE id=?",
                (utc_now(), job_id),
            )
        return result
