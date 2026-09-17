from __future__ import annotations

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

    async def create(self, source_url: str) -> dict[str, Any]:
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
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.post(
                f"{self.settings.metube_url}/jobs",
                headers=self._headers(),
                json={
                    "url": source_url,
                    "download_type": "audio",
                    "format": "mp3",
                    "codec": "mp3",
                    "quality": "best",
                },
            )
            response.raise_for_status()
            remote = response.json()
        job_id = str(uuid4())
        now = utc_now()
        poll_path = str(remote.get("poll_url", ""))
        if not re.fullmatch(r"/jobs/[0-9a-f-]{36}", poll_path):
            raise RuntimeError("MeTube returned an unsafe job path")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO downloads VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
                (job_id, remote["id"], remote["status"], poll_path, now, now),
            )
        return self.get_local(job_id)

    def get_local(self, job_id: str) -> dict[str, Any]:
        row = self.database.fetch_one("SELECT * FROM downloads WHERE id=?", (job_id,))
        if not row:
            raise KeyError("download job not found")
        return row

    async def refresh(self, job_id: str) -> dict[str, Any]:
        job = self.get_local(job_id)
        if job["status"] == "imported":
            return job
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get(
                f"{self.settings.metube_url}{job['poll_path']}", headers=self._headers()
            )
            response.raise_for_status()
            remote = response.json()
        file_path = remote.get("file_url")
        if file_path and not re.fullmatch(r"/jobs/[0-9a-f-]{36}/file", file_path):
            raise RuntimeError("MeTube returned an unsafe file path")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE downloads SET status=?, file_path=?, error=?, updated_at=? WHERE id=?",
                (remote["status"], file_path, remote.get("error"), utc_now(), job_id),
            )
        return self.get_local(job_id)

    async def import_completed(self, job_id: str) -> dict[str, Any]:
        local = self.get_local(job_id)
        if local["status"] == "imported":
            raise RuntimeError("Download has already been imported")
        job = await self.refresh(job_id)
        if job["status"] != "complete" or not job["file_path"]:
            raise RuntimeError("Download is not complete")
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
                declared = int(response.headers.get("content-length", "0") or 0)
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
            with temporary.open("rb") as source:
                result = self.library.add_stream(source, filename)
        finally:
            temporary.unlink(missing_ok=True)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE downloads SET status='imported', updated_at=? WHERE id=?",
                (utc_now(), job_id),
            )
        return result
