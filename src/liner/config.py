from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_data_path


@dataclass(frozen=True)
class Settings:
    library_root: Path
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8764
    max_upload_bytes: int = 500 * 1024 * 1024
    ai_base_url: str | None = None
    ai_model: str | None = None
    ai_api_key: str | None = field(default=None, repr=False)
    metube_url: str | None = None
    metube_cf_client_id: str | None = field(default=None, repr=False)
    metube_cf_client_secret: str | None = field(default=None, repr=False)
    metube_api_key: str | None = field(default=None, repr=False)
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)

    @classmethod
    def from_env(cls) -> Settings:
        root_value = os.getenv("LINER_LIBRARY_ROOT", "").strip()
        library_root = (
            Path(root_value).expanduser()
            if root_value
            else Path.home() / "Music" / "Local Files"
        )
        data_value = os.getenv("LINER_DATA_DIR", "").strip()
        data_dir = (
            Path(data_value).expanduser()
            if data_value
            else Path(user_data_path("Liner", appauthor=False))
        )
        return cls(
            library_root=library_root,
            data_dir=data_dir,
            port=int(os.getenv("LINER_PORT", "8764")),
            max_upload_bytes=int(os.getenv("LINER_MAX_UPLOAD_BYTES", str(500 * 1024 * 1024))),
            ai_base_url=os.getenv("LINER_AI_BASE_URL") or None,
            ai_model=os.getenv("LINER_AI_MODEL") or None,
            ai_api_key=os.getenv("LINER_AI_API_KEY") or None,
            metube_url=(os.getenv("LINER_METUBE_SIDECAR_URL") or "").rstrip("/") or None,
            metube_cf_client_id=os.getenv("LINER_METUBE_CF_CLIENT_ID") or None,
            metube_cf_client_secret=os.getenv("LINER_METUBE_CF_CLIENT_SECRET") or None,
            metube_api_key=os.getenv("LINER_METUBE_API_KEY") or None,
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "liner.sqlite3"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_base_url and self.ai_model and self.ai_api_key)

    @property
    def metube_enabled(self) -> bool:
        return bool(self.metube_url)

    def prepare(self) -> None:
        self.library_root.mkdir(parents=True, exist_ok=True)
        library = self.library_root.resolve()
        data = self.data_dir.resolve()
        if data == library or data.is_relative_to(library) or library.is_relative_to(data):
            raise ValueError("LINER_DATA_DIR and LINER_LIBRARY_ROOT must not overlap")
        for directory in (
            self.data_dir,
            self.backup_dir,
            self.quarantine_dir,
            self.staging_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
