from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any, ClassVar, Self

import httpx
import pytest
from conftest import make_audio
from fastapi.testclient import TestClient

from liner.config import Settings
from liner.media import MediaError, safe_library_path
from liner.metube import MeTubeService


def first_track(client: TestClient) -> dict:
    response = client.get("/api/tracks")
    assert response.status_code == 200
    return response.json()["items"][0]


def test_bootstrap_scans_library_and_hides_absolute_path(client: TestClient) -> None:
    response = client.get("/api/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert body["stats"]["active"] == 1
    assert body["library_name"] == "library"
    assert "Users" not in response.text


def test_rejects_unexpected_host_and_cross_origin_mutation(
    client: TestClient, mutation_headers: dict[str, str]
) -> None:
    assert client.get("/health", headers={"Host": "attacker.example"}).status_code == 400
    assert client.post("/api/scan").status_code == 403
    assert (
        client.post(
            "/api/scan",
            headers={**mutation_headers, "Origin": "https://attacker.example"},
        ).status_code
        == 403
    )


def test_edits_tags_and_filename_with_backup(
    client: TestClient, mutation_headers: dict[str, str], app_settings: Settings
) -> None:
    track = first_track(client)
    tags = {**track["tags"], "title": "Corrected Tone", "artist": "Test Artist"}

    response = client.patch(
        f"/api/tracks/{track['id']}",
        headers=mutation_headers,
        json={"tags": tags, "filename": "Test Artist - Corrected Tone.mp3"},
    )

    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["tags"]["title"] == "Corrected Tone"
    assert updated["filename"] == "Test Artist - Corrected Tone.mp3"
    assert (app_settings.library_root / updated["filename"]).is_file()
    assert len(list(app_settings.backup_dir.iterdir())) == 1


def test_quarantine_restore_and_permanent_delete(
    client: TestClient, mutation_headers: dict[str, str], app_settings: Settings
) -> None:
    track = first_track(client)
    quarantined = client.post(f"/api/tracks/{track['id']}/quarantine", headers=mutation_headers)
    assert quarantined.status_code == 200
    assert not (app_settings.library_root / track["filename"]).exists()

    restored = client.post(f"/api/tracks/{track['id']}/restore", headers=mutation_headers)
    assert restored.status_code == 200
    assert (app_settings.library_root / track["filename"]).is_file()

    client.post(f"/api/tracks/{track['id']}/quarantine", headers=mutation_headers)
    purged = client.delete(f"/api/tracks/{track['id']}", headers=mutation_headers)
    assert purged.status_code == 204
    assert not list(app_settings.quarantine_dir.iterdir())


def test_quarantined_path_can_be_reused_without_corrupting_index(
    client: TestClient,
    mutation_headers: dict[str, str],
    app_settings: Settings,
    tmp_path: Path,
) -> None:
    original = first_track(client)
    client.post(f"/api/tracks/{original['id']}/quarantine", headers=mutation_headers)
    replacement = make_audio(tmp_path / original["filename"])

    uploaded = client.post(
        "/api/uploads",
        headers=mutation_headers,
        files={
            "audio_file": (
                original["filename"],
                io.BytesIO(replacement.read_bytes()),
                "audio/mpeg",
            )
        },
    )

    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["relative_path"] == original["relative_path"]
    assert (
        client.post(
            f"/api/tracks/{original['id']}/restore", headers=mutation_headers
        ).status_code
        == 409
    )
    assert (app_settings.library_root / original["filename"]).is_file()


def test_upload_validates_and_indexes_audio(
    client: TestClient,
    mutation_headers: dict[str, str],
    app_settings: Settings,
    tmp_path: Path,
) -> None:
    source = make_audio(tmp_path / "new-tone.m4a")
    response = client.post(
        "/api/uploads",
        headers=mutation_headers,
        files={"audio_file": ("New Tone.m4a", io.BytesIO(source.read_bytes()), "audio/mp4")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["filename"] == "New Tone.m4a"
    assert (app_settings.library_root / "New Tone.m4a").is_file()


def test_filename_quality_is_not_classified_by_local_heuristics(
    client: TestClient,
    mutation_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    make_audio(app_settings.library_root / "Artist - Official Audio [abcdefghijk].mp3")
    response = client.post("/api/scan", headers=mutation_headers)
    assert response.status_code == 200
    tracks = client.get("/api/tracks").json()["items"]
    added = next(track for track in tracks if track["filename"].startswith("Artist - Official"))
    assert "noisy_filename" not in added["issues"]

    with client.app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE tracks SET issues_json=? WHERE id=?",
            ('["missing_artwork", "noisy_filename"]', added["id"]),
        )
    client.post("/api/scan", headers=mutation_headers)
    refreshed = client.get(f"/api/tracks/{added['id']}").json()
    assert refreshed["issues"] == ["missing_artwork"]


def test_upload_rejects_fake_audio(
    client: TestClient, mutation_headers: dict[str, str], app_settings: Settings
) -> None:
    response = client.post(
        "/api/uploads",
        headers=mutation_headers,
        files={"audio_file": ("fake.mp3", b"not audio", "audio/mpeg")},
    )
    assert response.status_code == 400
    assert not (app_settings.library_root / "fake.mp3").exists()
    assert not list(app_settings.staging_dir.iterdir())


def test_metube_replaces_missing_metadata_track_with_best_audio_and_undoes(
    client: TestClient,
    mutation_headers: dict[str, str],
    app_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = first_track(client)
    missing_tags = {**original["tags"], "artist": ""}
    repaired = client.patch(
        f"/api/tracks/{original['id']}",
        headers=mutation_headers,
        json={"tags": missing_tags, "filename": original["filename"]},
    ).json()
    assert "missing_artist" in repaired["issues"]
    downloaded = make_audio(
        tmp_path / "download.m4a", title="Replacement Title", artist="Replacement Artist"
    ).read_bytes()
    remote_id = "11111111-1111-1111-1111-111111111111"

    class FakeResponse:
        def __init__(
            self,
            *,
            payload: dict[str, Any] | None = None,
            content: bytes = b"",
            headers: dict[str, str] | None = None,
        ) -> None:
            self.payload = payload
            self.content = content
            self.headers = headers or {}
            self.request = httpx.Request("GET", "https://metube.example.com")

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            assert self.payload is not None
            return self.payload

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def aiter_bytes(self, _: int):
            yield self.content

    class FakeAsyncClient:
        submitted: ClassVar[dict[str, Any]] = {}

        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *_: Any, json: dict[str, Any], **__: Any) -> FakeResponse:
            self.submitted = json
            FakeAsyncClient.submitted = json
            return FakeResponse(
                payload={
                    "id": remote_id,
                    "status": "queued",
                    "poll_url": f"/jobs/{remote_id}",
                }
            )

        async def get(self, *_: Any, **__: Any) -> FakeResponse:
            return FakeResponse(
                payload={
                    "id": remote_id,
                    "status": "complete",
                    "poll_url": f"/jobs/{remote_id}",
                    "file_url": f"/jobs/{remote_id}/file",
                    "error": None,
                }
            )

        def stream(self, *_: Any, **__: Any) -> FakeResponse:
            return FakeResponse(
                content=downloaded,
                headers={
                    "content-length": str(len(downloaded)),
                    "content-disposition": 'attachment; filename="Replacement.m4a"',
                },
            )

    app_settings.metube_url = "https://metube.example.com"
    monkeypatch.setattr("liner.metube.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        "liner.metube.socket.getaddrinfo",
        lambda *_: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    created = client.post(
        f"/api/tracks/{original['id']}/replacement-jobs",
        headers=mutation_headers,
        json={"url": "https://media.example.com/track"},
    )
    assert created.status_code == 202, created.text
    job = created.json()
    assert job["purpose"] == "replace"
    assert job["target_track_id"] == original["id"]
    assert FakeAsyncClient.submitted["download_type"] == "audio"
    assert FakeAsyncClient.submitted["quality"] == "best"

    imported = client.post(f"/api/metube/jobs/{job['id']}/import", headers=mutation_headers)
    assert imported.status_code == 201, imported.text
    replacement = imported.json()
    assert replacement["id"] == original["id"]
    assert replacement["filename"].endswith(".m4a")
    assert replacement["tags"]["artist"] == "Replacement Artist"
    assert not (app_settings.library_root / original["filename"]).exists()

    operation = client.get("/api/history").json()["items"][0]
    assert operation["kind"] == "replace"
    undone = client.post(f"/api/history/{operation['id']}/undo", headers=mutation_headers)
    assert undone.status_code == 200, undone.text
    assert undone.json()["filename"] == original["filename"]
    assert (app_settings.library_root / original["filename"]).is_file()


def test_replacement_undo_refuses_to_overwrite_a_later_edit(
    client: TestClient,
    mutation_headers: dict[str, str],
    app_settings: Settings,
    tmp_path: Path,
) -> None:
    original = first_track(client)
    missing_tags = {**original["tags"], "artist": ""}
    client.patch(
        f"/api/tracks/{original['id']}",
        headers=mutation_headers,
        json={"tags": missing_tags, "filename": original["filename"]},
    )
    replacement_file = make_audio(
        tmp_path / "replacement.mp3", title="Replacement", artist="Artist"
    )
    with replacement_file.open("rb") as source:
        replacement = client.app.state.library.replace_stream(
            original["id"], source, replacement_file.name
        )
    replacement_operation = client.app.state.library.history()[0]
    edited_tags = {**replacement["tags"], "genre": "Later edit"}
    client.patch(
        f"/api/tracks/{original['id']}",
        headers=mutation_headers,
        json={"tags": edited_tags, "filename": replacement["filename"]},
    )

    response = client.post(
        f"/api/history/{replacement_operation['id']}/undo", headers=mutation_headers
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "A newer change must be undone first"
    assert client.get(f"/api/tracks/{original['id']}").json()["tags"]["genre"] == "Later edit"


def test_replacement_rejects_download_without_core_metadata(
    client: TestClient,
    mutation_headers: dict[str, str],
    app_settings: Settings,
    tmp_path: Path,
) -> None:
    original = first_track(client)
    missing_tags = {**original["tags"], "artist": ""}
    client.patch(
        f"/api/tracks/{original['id']}",
        headers=mutation_headers,
        json={"tags": missing_tags, "filename": original["filename"]},
    )
    original_path = app_settings.library_root / original["filename"]
    original_bytes = original_path.read_bytes()
    untagged = make_audio(tmp_path / "untagged.mp3", title=None, artist=None)

    with untagged.open("rb") as source, pytest.raises(MediaError, match="still lacks"):
        client.app.state.library.replace_stream(original["id"], source, untagged.name)

    assert original_path.read_bytes() == original_bytes
    assert not any(item["kind"] == "replace" for item in client.app.state.library.history())


def test_download_import_cannot_be_claimed_twice(
    client: TestClient, mutation_headers: dict[str, str]
) -> None:
    with client.app.state.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO downloads (
                id, remote_id, status, poll_path, created_at, updated_at,
                target_track_id, purpose
            ) VALUES (
                'local-job', 'remote-job', 'importing', '/jobs/remote-job',
                'now', 'now', NULL, 'add'
            )
            """
        )

    response = client.post("/api/metube/jobs/local-job/import", headers=mutation_headers)

    assert response.status_code == 409
    assert response.json()["detail"] == "Download is already being imported"


def test_delayed_refresh_does_not_release_an_import_claim(
    client: TestClient,
    app_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_id = "22222222-2222-2222-2222-222222222222"
    with client.app.state.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO downloads (
                id, remote_id, status, poll_path, created_at, updated_at,
                target_track_id, purpose
            ) VALUES (
                'race-job', ?, 'complete', ?, 'now', 'now', NULL, 'add'
            )
            """,
            (remote_id, f"/jobs/{remote_id}"),
        )

    class RaceResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            with client.app.state.database.transaction() as connection:
                connection.execute(
                    "UPDATE downloads SET status='importing' WHERE id='race-job'"
                )
            return {
                "id": remote_id,
                "status": "complete",
                "file_url": f"/jobs/{remote_id}/file",
                "error": None,
            }

    class RaceClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, *_: Any, **__: Any) -> RaceResponse:
            return RaceResponse()

    app_settings.metube_url = "https://metube.example.com"
    monkeypatch.setattr("liner.metube.httpx.AsyncClient", RaceClient)
    service = MeTubeService(app_settings, client.app.state.database, client.app.state.library)

    refreshed = asyncio.run(service.refresh("race-job"))

    assert refreshed["status"] == "importing"


def test_path_boundary_rejects_windows_escape_forms(app_settings: Settings) -> None:
    app_settings.prepare()
    for unsafe in ("../outside.mp3", "..\\outside.mp3", "C:\\outside.mp3", "file.mp3:stream"):
        try:
            safe_library_path(app_settings.library_root, unsafe, must_exist=False)
        except MediaError:
            pass
        else:
            raise AssertionError(f"unsafe path accepted: {unsafe}")


def test_ai_is_optional_and_fails_closed(
    client: TestClient, mutation_headers: dict[str, str]
) -> None:
    track = first_track(client)
    response = client.post(
        "/api/ai/plans",
        headers=mutation_headers,
        json={"prompt": "Fix this track", "track_ids": [track["id"]]},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "AI is not configured"


def test_rejects_runtime_data_inside_library(tmp_path: Path) -> None:
    settings = Settings(
        library_root=tmp_path / "library",
        data_dir=tmp_path / "library" / ".liner",
    )
    with pytest.raises(ValueError, match="must not overlap"):
        settings.prepare()


def settings_payload(app_settings: Settings) -> dict:
    return {
        "library_root": str(app_settings.library_root),
        "port": 8764,
        "max_upload_mb": 250,
        "backup_retention_days": 30,
        "naming_template": "{artist} - {title}",
        "ai": {
            "base_url": "https://ai.example.com/v1",
            "model": "metadata-model",
            "api_key": None,
            "clear_api_key": False,
        },
        "metube": {
            "url": "https://metube.example.com",
            "format": "m4a",
            "quality": "256",
            "cf_client_id": None,
            "cf_client_secret": None,
            "api_key": None,
            "clear_cf_client_id": False,
            "clear_cf_client_secret": False,
            "clear_api_key": False,
        },
    }


def test_settings_encrypt_secrets_and_never_return_them(
    client: TestClient,
    mutation_headers: dict[str, str],
    app_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = settings_payload(app_settings)
    payload["ai"]["api_key"] = "private-ai-key"
    payload["metube"]["cf_client_secret"] = "private-cf-secret"

    response = client.put("/api/settings", headers=mutation_headers, json=payload)

    assert response.status_code == 200, response.text
    assert response.json()["ai"]["api_key_set"] is True
    assert "private" not in response.text
    saved = app_settings.settings_path.read_text(encoding="utf-8")
    assert "private-ai-key" not in saved
    assert "private-cf-secret" not in saved
    assert json.loads(saved)["ai"]["api_key"]
    monkeypatch.setenv("LINER_DATA_DIR", str(app_settings.data_dir))
    reloaded = Settings.from_env()
    assert reloaded.ai_api_key == "private-ai-key"
    assert reloaded.metube_cf_client_secret == "private-cf-secret"


def test_settings_port_change_requires_restart_and_validates_inputs(
    client: TestClient, mutation_headers: dict[str, str], app_settings: Settings
) -> None:
    payload = settings_payload(app_settings)
    payload["port"] = 9876
    response = client.put("/api/settings", headers=mutation_headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["active_port"] == 8764
    assert response.json()["port"] == 9876
    assert response.json()["restart_required"] is True

    payload["ai"]["base_url"] = "https://user:password@ai.example.com"
    assert (
        client.put("/api/settings", headers=mutation_headers, json=payload).status_code == 422
    )
    payload["ai"]["base_url"] = ""
    payload["naming_template"] = "{unsupported}"
    assert (
        client.put("/api/settings", headers=mutation_headers, json=payload).status_code == 422
    )


def test_settings_can_switch_library_without_modifying_source_audio(
    client: TestClient,
    mutation_headers: dict[str, str],
    app_settings: Settings,
    tmp_path: Path,
) -> None:
    old_file = app_settings.library_root / "Liner Tests - Synthetic Tone.mp3"
    new_library = tmp_path / "second-library"
    new_library.mkdir()
    make_audio(new_library / "Second Track.mp3")
    payload = settings_payload(app_settings)
    payload["library_root"] = str(new_library)

    response = client.put("/api/settings", headers=mutation_headers, json=payload)

    assert response.status_code == 200, response.text
    assert old_file.is_file()
    tracks = client.get("/api/tracks").json()["items"]
    assert [track["filename"] for track in tracks] == ["Second Track.mp3"]
