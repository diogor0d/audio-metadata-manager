from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_data_path


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(value: str, *, decrypt: bool = False) -> str:
    if os.name != "nt":
        raise RuntimeError("Secret storage requires Windows DPAPI")
    raw = base64.b64decode(value) if decrypt else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _DataBlob()
    function = (
        ctypes.windll.crypt32.CryptUnprotectData  # type: ignore[attr-defined]
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData  # type: ignore[attr-defined]
    )
    if not function(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), "Windows could not protect the setting")
    try:
        protected = ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)  # type: ignore[attr-defined]
    return protected.decode("utf-8") if decrypt else base64.b64encode(protected).decode("ascii")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _secret(config: dict[str, Any], key: str, fallback: str | None) -> str | None:
    encrypted = config.get(key)
    if isinstance(encrypted, str) and encrypted:
        try:
            return _dpapi(encrypted, decrypt=True)
        except (OSError, ValueError, RuntimeError):
            return None
    return fallback


@dataclass
class Settings:
    library_root: Path
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8764
    max_upload_bytes: int = 500 * 1024 * 1024
    backup_retention_days: int = 90
    naming_template: str = "{artist} - {title}"
    ai_base_url: str | None = None
    ai_model: str | None = None
    ai_api_key: str | None = field(default=None, repr=False)
    metube_url: str | None = None
    metube_format: str = "mp3"
    metube_quality: str = "best"
    metube_cf_client_id: str | None = field(default=None, repr=False)
    metube_cf_client_secret: str | None = field(default=None, repr=False)
    metube_api_key: str | None = field(default=None, repr=False)
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)

    @classmethod
    def from_env(cls) -> Settings:
        data_value = os.getenv("LINER_DATA_DIR", "").strip()
        data_dir = (
            Path(data_value).expanduser()
            if data_value
            else Path(user_data_path("Liner", appauthor=False))
        )
        config = _read_json(data_dir / "settings.json")
        ai = config.get("ai") if isinstance(config.get("ai"), dict) else {}
        metube = config.get("metube") if isinstance(config.get("metube"), dict) else {}
        root_value = str(
            config.get("library_root") or os.getenv("LINER_LIBRARY_ROOT", "")
        ).strip()
        library_root = (
            Path(root_value).expanduser()
            if root_value
            else Path.home() / "Music" / "Local Files"
        )
        return cls(
            library_root=library_root,
            data_dir=data_dir,
            port=int(config.get("port") or os.getenv("LINER_PORT", "8764")),
            max_upload_bytes=int(
                config.get("max_upload_bytes")
                or os.getenv("LINER_MAX_UPLOAD_BYTES", str(500 * 1024 * 1024))
            ),
            backup_retention_days=int(config.get("backup_retention_days") or 90),
            naming_template=str(config.get("naming_template") or "{artist} - {title}"),
            ai_base_url=str(ai.get("base_url") or os.getenv("LINER_AI_BASE_URL") or "") or None,
            ai_model=str(ai.get("model") or os.getenv("LINER_AI_MODEL") or "") or None,
            ai_api_key=_secret(ai, "api_key", os.getenv("LINER_AI_API_KEY") or None),
            metube_url=(
                str(metube.get("url") or os.getenv("LINER_METUBE_SIDECAR_URL") or "").rstrip(
                    "/"
                )
                or None
            ),
            metube_format=str(metube.get("format") or "mp3"),
            metube_quality=str(metube.get("quality") or "best"),
            metube_cf_client_id=_secret(
                metube, "cf_client_id", os.getenv("LINER_METUBE_CF_CLIENT_ID") or None
            ),
            metube_cf_client_secret=_secret(
                metube,
                "cf_client_secret",
                os.getenv("LINER_METUBE_CF_CLIENT_SECRET") or None,
            ),
            metube_api_key=_secret(
                metube, "api_key", os.getenv("LINER_METUBE_API_KEY") or None
            ),
        )

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

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
        self.validate_paths(self.library_root)
        for directory in (
            self.data_dir,
            self.backup_dir,
            self.quarantine_dir,
            self.staging_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def validate_paths(self, library_root: Path) -> None:
        library = library_root.resolve()
        data = self.data_dir.resolve()
        if data == library or data.is_relative_to(library) or library.is_relative_to(data):
            raise ValueError("Liner's data directory and library folder must not overlap")

    def public(self, *, active_port: int | None = None) -> dict[str, Any]:
        return {
            "library_root": str(self.library_root),
            "port": self.port,
            "active_port": active_port if active_port is not None else self.port,
            "max_upload_mb": self.max_upload_bytes // (1024 * 1024),
            "backup_retention_days": self.backup_retention_days,
            "naming_template": self.naming_template,
            "ai": {
                "base_url": self.ai_base_url or "",
                "model": self.ai_model or "",
                "api_key_set": bool(self.ai_api_key),
            },
            "metube": {
                "url": self.metube_url or "",
                "format": self.metube_format,
                "quality": self.metube_quality,
                "cf_client_id_set": bool(self.metube_cf_client_id),
                "cf_client_secret_set": bool(self.metube_cf_client_secret),
                "api_key_set": bool(self.metube_api_key),
            },
        }

    def persist(self) -> None:
        payload = self.public()
        payload.pop("active_port", None)
        payload["max_upload_bytes"] = self.max_upload_bytes
        payload.pop("max_upload_mb", None)
        ai = payload["ai"]
        ai.pop("api_key_set", None)
        ai["api_key"] = _dpapi(self.ai_api_key) if self.ai_api_key else ""
        metube = payload["metube"]
        metube.pop("cf_client_id_set", None)
        metube.pop("cf_client_secret_set", None)
        metube.pop("api_key_set", None)
        metube["cf_client_id"] = (
            _dpapi(self.metube_cf_client_id) if self.metube_cf_client_id else ""
        )
        metube["cf_client_secret"] = (
            _dpapi(self.metube_cf_client_secret) if self.metube_cf_client_secret else ""
        )
        metube["api_key"] = _dpapi(self.metube_api_key) if self.metube_api_key else ""
        temporary = self.settings_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.settings_path)
