from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from uuid import UUID

import httpx

from . import __version__
from .media import MediaError
from .service import LibraryService

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording"
COVER_ART_URL = "https://coverartarchive.org/release-group"
USER_AGENT = f"Liner/{__version__} (https://github.com/diogor0d/audio-metadata-manager)"
MAX_ARTWORK_BYTES = 10 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MBID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class ArtworkProviderError(RuntimeError):
    pass


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _artist_credit(recording: dict[str, Any]) -> str:
    parts = recording.get("artist-credit") or []
    if not isinstance(parts, list):
        return ""
    display = "".join(
        f"{part.get('name', '')}{part.get('joinphrase', '')}"
        for part in parts
        if isinstance(part, dict)
    )
    return display


def candidates_from_response(
    payload: dict[str, Any], *, artist: str, title: str, album: str, duration: float
) -> list[dict[str, Any]]:
    target_artist = _normalized(artist)
    target_title = _normalized(title)
    target_album = _normalized(album)
    candidates: dict[str, dict[str, Any]] = {}
    for recording in payload.get("recordings", [])[:15]:
        if not isinstance(recording, dict):
            continue
        credit = _artist_credit(recording)
        title_exact = _normalized(str(recording.get("title", ""))) == target_title
        artist_exact = _normalized(credit) == target_artist
        try:
            remote_duration = float(recording.get("length") or 0) / 1000
        except (TypeError, ValueError):
            remote_duration = 0
        duration_close = bool(
            remote_duration and duration and abs(remote_duration - duration) <= 4
        )
        releases = recording.get("releases") or []
        if not isinstance(releases, list):
            continue
        for release in releases:
            if not isinstance(release, dict):
                continue
            group = release.get("release-group") or {}
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("id", ""))
            if not MBID_PATTERN.fullmatch(group_id):
                continue
            release_title = str(group.get("title") or release.get("title") or "")
            album_exact = bool(target_album and _normalized(release_title) == target_album)
            try:
                score = int(recording.get("score") or 0)
            except (TypeError, ValueError):
                score = 0
            confidence = min(
                100,
                score
                + (8 if title_exact else 0)
                + (8 if artist_exact else 0)
                + (10 if album_exact else 0)
                + (4 if duration_close else 0),
            )
            candidate = {
                "release_group_id": group_id,
                "release": release_title,
                "artist": credit,
                "date": str(release.get("date") or recording.get("first-release-date") or ""),
                "type": str(group.get("primary-type") or "Release"),
                "confidence": confidence,
                "exact_track": title_exact and artist_exact and duration_close,
                "exact_album": album_exact,
            }
            previous = candidates.get(group_id)
            if previous is None or candidate["confidence"] > previous["confidence"]:
                candidates[group_id] = candidate
    return sorted(
        candidates.values(),
        key=lambda item: (
            item["exact_album"],
            item["exact_track"],
            item["confidence"],
            item["date"],
        ),
        reverse=True,
    )[:8]


def automatic_candidate(
    items: list[dict[str, Any]], *, album: str, complete_results: bool
) -> dict[str, Any] | None:
    if not album or not complete_results:
        return None
    exact = [
        item
        for item in items
        if item["confidence"] == 100 and item["exact_track"] and item["exact_album"]
    ]
    return exact[0] if len(exact) == 1 else None


def _safe_artwork_url(value: str) -> str:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"coverartarchive.org", "archive.org"} and not hostname.endswith(
        ".archive.org"
    ):
        raise RuntimeError("Artwork provider returned an unsafe redirect")
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise RuntimeError("Artwork provider returned an unsafe redirect")
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    return urlunparse(parsed)


class ArtworkService:
    def __init__(self, library: LibraryService):
        self.library = library
        self._musicbrainz_lock = asyncio.Lock()
        self._last_musicbrainz_request = 0.0
        self._candidates: dict[str, tuple[float, set[str]]] = {}

    async def discover(self, track_id: str) -> dict[str, Any]:
        track, _ = self.library.resolve_active(track_id)
        artist = track["tags"]["artist"].strip()
        title = track["tags"]["title"].strip()
        album = track["tags"]["album"].strip()
        if not artist or not title:
            raise MediaError("Add artist and title metadata before searching for artwork")
        query = f'recording:"{_quoted(title)}" AND artist:"{_quoted(artist)}"'
        if album:
            query += f' AND release:"{_quoted(album)}"'
        async with self._musicbrainz_lock:
            for attempt in range(2):
                delay = 1.05 - (time.monotonic() - self._last_musicbrainz_request)
                if delay > 0:
                    await asyncio.sleep(delay)
                async with (
                    httpx.AsyncClient(timeout=20, follow_redirects=False) as client,
                    client.stream(
                        "GET",
                        MUSICBRAINZ_URL,
                        params={"query": query, "fmt": "json", "limit": "15"},
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    ) as response,
                ):
                    status_code = response.status_code
                    if status_code == 200:
                        payload = await self._bounded_json(response)
                self._last_musicbrainz_request = time.monotonic()
                if status_code != 503 or attempt == 1:
                    break
        if status_code == 503:
            raise RuntimeError("MusicBrainz is busy; try again shortly")
        if status_code != 200:
            raise RuntimeError(f"MusicBrainz returned HTTP {status_code}")
        items = candidates_from_response(
            payload,
            artist=artist,
            title=title,
            album=album,
            duration=float(track["duration"] or 0),
        )
        if items:
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
                available = await asyncio.gather(
                    *(self._has_front_art(client, item["release_group_id"]) for item in items)
                )
            items = [item for item, has_art in zip(items, available, strict=True) if has_art]
        self._candidates[track_id] = (
            time.monotonic() + 15 * 60,
            {item["release_group_id"] for item in items},
        )
        try:
            result_count = int(payload.get("count") or 0)
        except (TypeError, ValueError):
            result_count = 16
        automatic = automatic_candidate(items, album=album, complete_results=result_count <= 15)
        return {"items": items, "automatic": automatic}

    @staticmethod
    async def _bounded_json(response: httpx.Response) -> dict[str, Any]:
        try:
            declared = int(response.headers.get("content-length", "0") or 0)
        except ValueError as exc:
            raise RuntimeError("MusicBrainz returned an invalid response size") from exc
        if declared > MAX_METADATA_BYTES:
            raise RuntimeError("MusicBrainz response exceeded Liner's metadata limit")
        content = bytearray()
        async for chunk in response.aiter_bytes(64 * 1024):
            content.extend(chunk)
            if len(content) > MAX_METADATA_BYTES:
                raise RuntimeError("MusicBrainz response exceeded Liner's metadata limit")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("MusicBrainz returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("recordings", []), list):
            raise ArtworkProviderError("MusicBrainz response schema was invalid")
        return payload

    async def _has_front_art(self, client: httpx.AsyncClient, release_group_id: str) -> bool:
        try:
            response = await client.head(
                f"{COVER_ART_URL}/{release_group_id}/front-500",
                headers={"User-Agent": USER_AGENT},
            )
            if response.status_code == 404:
                return False
            if response.status_code in {200, 301, 302, 303, 307, 308}:
                return True
            response.raise_for_status()
            return False
        except httpx.RequestError as exc:
            raise RuntimeError("Cover Art Archive is temporarily unavailable") from exc

    def _authorize_candidate(self, track_id: str, release_group_id: str) -> None:
        try:
            UUID(release_group_id)
        except ValueError as exc:
            raise MediaError("Invalid artwork candidate") from exc
        cached = self._candidates.get(track_id)
        if not cached or cached[0] < time.monotonic() or release_group_id not in cached[1]:
            raise MediaError("Artwork candidates expired; search again")

    async def preview(self, track_id: str, release_group_id: str) -> tuple[bytes, str]:
        self.library.resolve_active(track_id)
        self._authorize_candidate(track_id, release_group_id)
        return await self._download(release_group_id, size=500)

    async def apply(self, track_id: str, release_group_id: str) -> dict[str, Any]:
        self._authorize_candidate(track_id, release_group_id)
        image, mime = await self._download(release_group_id, size=1200)
        return await asyncio.to_thread(self.library.update_artwork, track_id, image, mime)

    async def apply_automatic(self, track_id: str) -> dict[str, Any]:
        result = await self.discover(track_id)
        automatic = result["automatic"]
        if not automatic:
            return result
        track = await self.apply(track_id, automatic["release_group_id"])
        return {**result, "applied": True, "track": track}

    async def _download(self, release_group_id: str, *, size: int) -> tuple[bytes, str]:
        url = f"{COVER_ART_URL}/{release_group_id}/front-{size}"
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for _ in range(4):
                safe_url = _safe_artwork_url(url)
                async with client.stream(
                    "GET", safe_url, headers={"User-Agent": USER_AGENT}
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise RuntimeError("Artwork provider returned an empty redirect")
                        url = urljoin(safe_url, location)
                        continue
                    if response.status_code == 404:
                        raise MediaError("This artwork is no longer available")
                    response.raise_for_status()
                    mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if mime not in {"image/jpeg", "image/png"}:
                        raise RuntimeError("Artwork provider returned an unsupported file")
                    try:
                        declared = int(response.headers.get("content-length", "0") or 0)
                    except ValueError as exc:
                        raise RuntimeError(
                            "Artwork provider returned an invalid file size"
                        ) from exc
                    if declared > MAX_ARTWORK_BYTES:
                        raise RuntimeError("Artwork exceeds Liner's 10 MB limit")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes(256 * 1024):
                        chunks.extend(chunk)
                        if len(chunks) > MAX_ARTWORK_BYTES:
                            raise RuntimeError("Artwork exceeds Liner's 10 MB limit")
                    return bytes(chunks), mime
        raise RuntimeError("Artwork provider redirected too many times")
