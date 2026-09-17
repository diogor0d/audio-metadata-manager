from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from conftest import make_audio
from fastapi.testclient import TestClient

from liner.config import Settings
from liner.media import MediaError, safe_library_path


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
