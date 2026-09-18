from __future__ import annotations

import io
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from liner.artwork import (
    MAX_METADATA_BYTES,
    ArtworkService,
    _safe_artwork_url,
    automatic_candidate,
    candidates_from_response,
)

RELEASE_GROUP_ID = "c31a5e2b-0bf8-32e0-8aeb-ef4ba9973932"


def test_musicbrainz_candidates_require_semantic_and_duration_match() -> None:
    payload = {
        "recordings": [
            {
                "score": 100,
                "title": "Example Song",
                "length": 180000,
                "artist-credit": [{"name": "Example Artist"}],
                "releases": [
                    {
                        "id": "76df3287-6cda-33eb-8e9a-044b5e15ffdd",
                        "title": "Example Album",
                        "date": "2026-01-02",
                        "release-group": {
                            "id": RELEASE_GROUP_ID,
                            "title": "Example Album",
                            "primary-type": "Album",
                        },
                    }
                ],
            }
        ]
    }

    candidates = candidates_from_response(
        payload,
        artist="Example Artist",
        title="Example Song",
        album="Example Album",
        duration=181,
    )

    assert candidates == [
        {
            "release_group_id": RELEASE_GROUP_ID,
            "release": "Example Album",
            "artist": "Example Artist",
            "date": "2026-01-02",
            "type": "Album",
            "confidence": 100,
            "exact_track": True,
            "exact_album": True,
        }
    ]


def test_artwork_redirects_are_restricted_to_archive_hosts() -> None:
    assert (
        _safe_artwork_url("http://ia800000.us.archive.org/file.jpg")
        == "https://ia800000.us.archive.org/file.jpg"
    )
    with pytest.raises(RuntimeError, match="unsafe redirect"):
        _safe_artwork_url("https://attacker.example/file.jpg")
    with pytest.raises(RuntimeError, match="unsafe redirect"):
        _safe_artwork_url("https://archive.org.attacker.example/file.jpg")


def test_automatic_artwork_requires_complete_album_match() -> None:
    item = {
        "confidence": 100,
        "exact_track": True,
        "exact_album": True,
        "release_group_id": RELEASE_GROUP_ID,
    }
    assert automatic_candidate([item], album="Album", complete_results=True) == item
    assert automatic_candidate([item], album="", complete_results=True) is None
    assert automatic_candidate([item], album="Album", complete_results=False) is None
    assert automatic_candidate([item, item], album="Album", complete_results=True) is None


@pytest.mark.asyncio
async def test_musicbrainz_metadata_response_is_bounded_and_validated() -> None:
    oversized = httpx.Response(
        200,
        headers={"content-length": str(MAX_METADATA_BYTES + 1)},
        content=b"{}",
    )
    with pytest.raises(RuntimeError, match="metadata limit"):
        await ArtworkService._bounded_json(oversized)

    malformed = httpx.Response(200, content=b"[]")
    with pytest.raises(RuntimeError, match="schema was invalid"):
        await ArtworkService._bounded_json(malformed)


def test_artwork_discovery_and_selection_routes(
    client: TestClient,
    mutation_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = client.get("/api/tracks").json()["items"][0]
    candidate = {
        "release_group_id": RELEASE_GROUP_ID,
        "release": "Example Album",
        "artist": "Example Artist",
        "date": "2026",
        "type": "Album",
        "confidence": 92,
        "exact_track": True,
        "exact_album": False,
    }
    lookup = client.app.state.artwork_lookup
    monkeypatch.setattr(
        lookup,
        "apply_automatic",
        AsyncMock(return_value={"items": [candidate], "automatic": None}),
    )
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "navy").save(image, format="JPEG")
    artwork = image.getvalue()
    monkeypatch.setattr(lookup, "_download", AsyncMock(return_value=(artwork, "image/jpeg")))
    lookup._candidates[track["id"]] = (time.monotonic() + 60, {RELEASE_GROUP_ID})

    discovered = client.post(
        f"/api/tracks/{track['id']}/artwork/discover", headers=mutation_headers
    )
    assert discovered.status_code == 200
    assert discovered.json()["items"] == [candidate]
    preview = client.get(f"/api/tracks/{track['id']}/artwork/candidates/{RELEASE_GROUP_ID}")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"
    applied = client.post(
        f"/api/tracks/{track['id']}/artwork/from-provider",
        headers=mutation_headers,
        json={"release_group_id": RELEASE_GROUP_ID},
    )
    assert applied.status_code == 200
    assert applied.json()["has_artwork"] is True
    history = client.get("/api/history").json()["items"]
    assert history[0]["kind"] == "artwork"
    undone = client.post(f"/api/history/{history[0]['id']}/undo", headers=mutation_headers)
    assert undone.status_code == 200
    restored = client.get(f"/api/tracks/{track['id']}").json()
    assert restored["has_artwork"] is False
