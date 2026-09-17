from __future__ import annotations

import mimetypes
import string
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .ai import AIService
from .config import Settings
from .db import Database
from .media import TAG_FIELDS, MediaError, extract_artwork
from .metube import MeTubeService
from .service import LibraryService


class TrackUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tags: dict[str, str]
    filename: str | None = Field(default=None, max_length=180)


class NamingPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1, max_length=160)


class AIRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4000)
    track_ids: list[str] = Field(default_factory=list, max_length=50)


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=4096)


class AISettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(default="", max_length=2048)
    model: str = Field(default="", max_length=200)
    api_key: str | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False


class MeTubeSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(default="", max_length=2048)
    format: str = Field(default="mp3", pattern="^(mp3|m4a|opus)$")
    quality: str = Field(default="best", pattern="^(best|320|256|192|128)$")
    cf_client_id: str | None = Field(default=None, max_length=4096)
    cf_client_secret: str | None = Field(default=None, max_length=4096)
    api_key: str | None = Field(default=None, max_length=4096)
    clear_cf_client_id: bool = False
    clear_cf_client_secret: bool = False
    clear_api_key: bool = False


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_root: str = Field(min_length=1, max_length=1024)
    port: int = Field(ge=1024, le=65535)
    max_upload_mb: int = Field(ge=1, le=4096)
    backup_retention_days: int = Field(ge=1, le=3650)
    naming_template: str = Field(min_length=1, max_length=160)
    ai: AISettingsUpdate
    metube: MeTubeSettingsUpdate


def _integration_url(value: str, name: str) -> str | None:
    value = value.strip().rstrip("/")
    if not value:
        return None
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=422, detail=f"{name} must be an HTTP(S) origin")
    return value


def _validate_template(value: str) -> str:
    allowed = {"artist", "title", "album", "albumartist", "date", "year", "genre", "track"}
    try:
        fields = {field for _, field, _, _ in string.Formatter().parse(value) if field}
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Naming template has invalid braces"
        ) from exc
    if not fields or not fields.issubset(allowed):
        raise HTTPException(
            status_code=422,
            detail="Naming template must use supported metadata fields",
        )
    return value.strip()


class LocalBoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, settings: Settings, allow_test_host: bool = False):
        super().__init__(app)
        self.settings = settings
        self.allowed_hosts = {
            f"127.0.0.1:{settings.port}",
            f"localhost:{settings.port}",
        }
        if allow_test_host:
            self.allowed_hosts.add("testserver")

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        host = request.headers.get("host", "").lower()
        if host not in self.allowed_hosts:
            return JSONResponse({"detail": "Invalid host"}, status_code=400)
        if request.url.path.startswith("/api/") and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            origin = request.headers.get("origin")
            allowed_origins = {f"http://{item}" for item in self.allowed_hosts}
            if origin and origin.lower() not in allowed_origins:
                return JSONResponse({"detail": "Invalid request origin"}, status_code=403)
            if request.headers.get("x-liner-request") != self.settings.csrf_token:
                return JSONResponse({"detail": "Invalid request token"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response


def create_app(settings: Settings | None = None, *, allow_test_host: bool = False) -> FastAPI:
    active_settings = settings or Settings.from_env()
    active_settings.prepare()
    database = Database(active_settings.database_path)
    database.initialize()
    library = LibraryService(active_settings, database)
    ai = AIService(active_settings, database, library)
    metube = MeTubeService(active_settings, database, library)
    active_port = active_settings.port

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        library.scan()
        library.cleanup_backups()
        yield

    application = FastAPI(
        title="Liner",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(
        LocalBoundaryMiddleware, settings=active_settings, allow_test_host=allow_test_host
    )
    application.state.settings = active_settings
    application.state.database = database
    application.state.library = library

    @application.exception_handler(MediaError)
    async def media_error(_: Request, exc: MediaError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @application.exception_handler(FileExistsError)
    async def conflict_error(_: Request, exc: FileExistsError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @application.exception_handler(KeyError)
    async def not_found_error(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": exc.args[0]}, status_code=404)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @application.get("/api/bootstrap")
    async def bootstrap() -> dict[str, Any]:
        return {
            "version": __version__,
            "csrf_token": active_settings.csrf_token,
            "library_name": active_settings.library_root.name,
            "naming_template": active_settings.naming_template,
            "stats": library.stats(),
            "capabilities": {
                "ai": active_settings.ai_enabled,
                "metube": active_settings.metube_enabled,
            },
        }

    @application.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return active_settings.public(active_port=active_port)

    @application.put("/api/settings")
    async def update_settings(body: SettingsUpdate) -> dict[str, Any]:
        library_root = Path(body.library_root).expanduser()
        if not library_root.is_dir():
            raise HTTPException(status_code=422, detail="Library folder does not exist")
        try:
            active_settings.validate_paths(library_root)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        ai_url = _integration_url(body.ai.base_url, "AI endpoint")
        metube_url = _integration_url(body.metube.url, "MeTube endpoint")
        naming_template = _validate_template(body.naming_template)
        changed_library = library_root.resolve() != active_settings.library_root.resolve()
        if changed_library and library.stats()["quarantined"]:
            raise HTTPException(
                status_code=409,
                detail="Restore or delete quarantined files before changing libraries",
            )

        previous = active_settings.__dict__.copy()
        active_settings.library_root = library_root.resolve()
        active_settings.port = body.port
        active_settings.max_upload_bytes = body.max_upload_mb * 1024 * 1024
        active_settings.backup_retention_days = body.backup_retention_days
        active_settings.naming_template = naming_template
        active_settings.ai_base_url = ai_url
        active_settings.ai_model = body.ai.model.strip() or None
        if body.ai.clear_api_key:
            active_settings.ai_api_key = None
        elif body.ai.api_key is not None and body.ai.api_key.strip():
            active_settings.ai_api_key = body.ai.api_key.strip()
        active_settings.metube_url = metube_url
        active_settings.metube_format = body.metube.format
        active_settings.metube_quality = body.metube.quality
        for attribute, value, clear in (
            ("metube_cf_client_id", body.metube.cf_client_id, body.metube.clear_cf_client_id),
            (
                "metube_cf_client_secret",
                body.metube.cf_client_secret,
                body.metube.clear_cf_client_secret,
            ),
            ("metube_api_key", body.metube.api_key, body.metube.clear_api_key),
        ):
            if clear:
                setattr(active_settings, attribute, None)
            elif value is not None and value.strip():
                setattr(active_settings, attribute, value.strip())
        try:
            active_settings.persist()
        except (OSError, RuntimeError) as exc:
            active_settings.__dict__.update(previous)
            raise HTTPException(status_code=500, detail="Settings could not be saved") from exc
        if changed_library:
            library.reset_index()
            library.scan()
        library.cleanup_backups()
        return {
            **active_settings.public(active_port=active_port),
            "restart_required": body.port != active_port,
            "stats": library.stats(),
        }

    @application.post("/api/scan")
    async def scan() -> dict[str, Any]:
        return {**library.scan(), "stats": library.stats()}

    @application.get("/api/tracks")
    async def tracks(
        search: str = Query(default="", max_length=100),
        status: str = Query(default="active", pattern="^(active|quarantined)$"),
        issue: str = Query(default="", max_length=40),
        limit: int = Query(default=500, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return {
            "items": library.list_tracks(
                search=search, status=status, issue=issue, limit=limit, offset=offset
            ),
            "stats": library.stats(),
        }

    @application.get("/api/tracks/{track_id}")
    async def track(track_id: str) -> dict[str, Any]:
        return library.get_track(track_id)

    @application.patch("/api/tracks/{track_id}")
    async def update_track(track_id: str, body: TrackUpdate) -> dict[str, Any]:
        if any(key not in TAG_FIELDS for key in body.tags):
            raise HTTPException(status_code=422, detail="Unsupported metadata field")
        return library.update_track(track_id, body.tags, body.filename)

    @application.post("/api/tracks/{track_id}/name-preview")
    async def preview_name(track_id: str, body: NamingPreview) -> dict[str, str]:
        return {"filename": library.preview_name(track_id, body.template)}

    @application.post("/api/tracks/{track_id}/artwork")
    async def update_artwork(
        track_id: str,
        artwork: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        content = await artwork.read(10 * 1024 * 1024 + 1)
        return library.update_artwork(track_id, content, artwork.content_type or "")

    @application.get("/api/tracks/{track_id}/artwork")
    async def artwork(track_id: str) -> Response:
        _, path = library.resolve_active(track_id)
        result = extract_artwork(path)
        if not result:
            raise HTTPException(status_code=404, detail="Artwork not found")
        content, media_type = result
        return Response(content, media_type=media_type)

    @application.get("/api/tracks/{track_id}/audio")
    async def audio(track_id: str) -> FileResponse:
        _, path = library.resolve_active(track_id)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(
            path, media_type=media_type, filename=path.name, content_disposition_type="inline"
        )

    @application.post("/api/tracks/{track_id}/quarantine")
    async def quarantine(track_id: str) -> dict[str, Any]:
        return library.quarantine(track_id)

    @application.post("/api/tracks/{track_id}/restore")
    async def restore(track_id: str) -> dict[str, Any]:
        return library.restore(track_id)

    @application.delete("/api/tracks/{track_id}", status_code=204)
    async def purge(track_id: str) -> Response:
        library.purge(track_id)
        return Response(status_code=204)

    @application.post("/api/uploads", status_code=201)
    async def upload(audio_file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        if not audio_file.filename:
            raise HTTPException(status_code=422, detail="A filename is required")
        return library.add_stream(audio_file.file, audio_file.filename)

    @application.get("/api/history")
    async def history() -> dict[str, Any]:
        return {"items": library.history()}

    @application.post("/api/history/{operation_id}/undo")
    async def undo(operation_id: str) -> dict[str, Any]:
        return library.undo(operation_id)

    @application.post("/api/ai/plans")
    async def create_ai_plan(body: AIRequest) -> dict[str, Any]:
        try:
            return await ai.create_plan(body.prompt, body.track_ids)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post("/api/ai/plans/{plan_id}/apply")
    async def apply_ai_plan(plan_id: str) -> dict[str, Any]:
        return ai.apply_plan(plan_id)

    @application.post("/api/metube/jobs", status_code=202)
    async def create_download(body: DownloadRequest) -> dict[str, Any]:
        try:
            return await metube.create(body.url)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.get("/api/metube/jobs/{job_id}")
    async def get_download(job_id: str) -> dict[str, Any]:
        return await metube.refresh(job_id)

    @application.post("/api/metube/jobs/{job_id}/import", status_code=201)
    async def import_download(job_id: str) -> dict[str, Any]:
        return await metube.import_completed(job_id)

    static_dir = Path(__file__).with_name("static")
    index_path = static_dir / "index.html"

    @application.get("/{requested_path:path}", include_in_schema=False, response_model=None)
    async def frontend(requested_path: str) -> Response:
        if requested_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = (static_dir / requested_path).resolve()
        if (
            requested_path
            and candidate.is_relative_to(static_dir.resolve())
            and candidate.is_file()
        ):
            return FileResponse(candidate)
        if index_path.is_file():
            return FileResponse(index_path)
        return JSONResponse(
            {"detail": "Frontend build is missing. Run npm run build in frontend."},
            status_code=503,
        )

    return application
