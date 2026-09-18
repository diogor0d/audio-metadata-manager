from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from liner.app import create_app
from liner.config import Settings


def make_audio(
    path: Path,
    *,
    duration: float = 0.2,
    title: str | None = "Synthetic Tone",
    artist: str | None = "Liner Tests",
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for synthetic audio fixtures")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
    ]
    if title is not None:
        command.extend(["-metadata", f"title={title}"])
    if artist is not None:
        command.extend(["-metadata", f"artist={artist}"])
    command.extend(["-y", str(path)])
    subprocess.run(
        command,
        check=True,
        timeout=20,
    )
    return path


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    return Settings(
        library_root=tmp_path / "library",
        data_dir=tmp_path / "data",
        port=8764,
        csrf_token="test-request-token",
    )


@pytest.fixture
def client(app_settings: Settings) -> TestClient:
    app_settings.prepare()
    make_audio(app_settings.library_root / "Liner Tests - Synthetic Tone.mp3")
    with TestClient(create_app(app_settings, allow_test_host=True)) as test_client:
        yield test_client


@pytest.fixture
def mutation_headers() -> dict[str, str]:
    return {"X-Liner-Request": "test-request-token"}
