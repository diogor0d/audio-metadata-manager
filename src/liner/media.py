from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path, PurePath
from typing import Any

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image

SUPPORTED_EXTENSIONS = {".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".opus", ".wav"}
TAG_FIELDS = ("title", "artist", "album", "albumartist", "date", "genre", "tracknumber")
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
TEMPLATE_TOKEN = re.compile(r"\{(artist|title|album|albumartist|date|year|genre|track)\}")


class MediaError(ValueError):
    pass


def _is_reparse_point(path: Path) -> bool:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return False
    return bool(getattr(stat, "st_file_attributes", 0) & 0x400) or path.is_symlink()


def ensure_safe_root(root: Path) -> Path:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or _is_reparse_point(resolved):
        raise MediaError("The library root must be a real local directory")
    return resolved


def safe_library_path(root: Path, relative_path: str, *, must_exist: bool = True) -> Path:
    if not relative_path or "\x00" in relative_path:
        raise MediaError("Invalid library path")
    candidate_input = Path(relative_path)
    if candidate_input.is_absolute() or candidate_input.drive or ":" in relative_path:
        raise MediaError("Only library-relative paths are accepted")
    if any(part in {"", ".", ".."} for part in PurePath(relative_path).parts):
        raise MediaError("Invalid library path")
    canonical_root = ensure_safe_root(root)
    candidate = canonical_root.joinpath(*PurePath(relative_path).parts)
    current = canonical_root
    for part in PurePath(relative_path).parts[:-1]:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise MediaError("Reparse points are not supported inside the library")
    resolved = candidate.resolve(strict=must_exist)
    if not resolved.is_relative_to(canonical_root):
        raise MediaError("Path escapes the configured library")
    if must_exist and (_is_reparse_point(candidate) or not resolved.is_file()):
        raise MediaError("The requested item is not a regular library file")
    return candidate


def sanitize_filename(value: str, extension: str | None = None) -> str:
    normalized = unicodedata.normalize("NFC", Path(value).name)
    normalized = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    if extension:
        suffix = extension.lower() if extension.startswith(".") else f".{extension.lower()}"
        stem = Path(normalized).stem if Path(normalized).suffix else normalized
        normalized = f"{stem}{suffix}"
    stem = Path(normalized).stem
    if not normalized or stem.upper() in RESERVED_NAMES:
        raise MediaError("Filename is empty or reserved by Windows")
    if len(normalized) > 180:
        suffix = Path(normalized).suffix
        normalized = f"{Path(normalized).stem[: 180 - len(suffix)]}{suffix}"
    return normalized


def unique_destination(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    counter = 2
    while candidate.exists():
        candidate = directory / f"{Path(filename).stem} ({counter}){Path(filename).suffix}"
        counter += 1
    return candidate


def _first(tags: Any, key: str) -> str:
    if not tags:
        return ""
    value = tags.get(key, "")
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def inspect_media(path: Path) -> dict[str, Any]:
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise MediaError(f"Unsupported audio format: {path.suffix.lower() or 'unknown'}")
    try:
        easy = mutagen.File(path, easy=True)
        full = mutagen.File(path, easy=False)
    except Exception as exc:
        raise MediaError("The file is not readable audio") from exc
    if easy is None or full is None or not getattr(full, "info", None):
        raise MediaError("The file is not recognizable audio")
    tags = {field: _first(easy.tags, field) for field in TAG_FIELDS}
    info = full.info
    has_artwork = False
    if isinstance(full, MP4):
        has_artwork = bool(full.tags and full.tags.get("covr"))
    elif isinstance(full, FLAC):
        has_artwork = bool(full.pictures)
    else:
        has_artwork = bool(
            full.tags and any(isinstance(frame, APIC) for frame in full.tags.values())
        )
    issues: list[str] = []
    if not tags["artist"]:
        issues.append("missing_artist")
    if not tags["title"]:
        issues.append("missing_title")
    if not has_artwork:
        issues.append("missing_artwork")
    return {
        "size": path.stat().st_size,
        "mtime_ns": path.stat().st_mtime_ns,
        "duration": round(float(getattr(info, "length", 0.0)), 3),
        "bitrate": int(getattr(info, "bitrate", 0) or 0),
        "format": path.suffix.lower().lstrip("."),
        "tags": tags,
        "issues": issues,
        "has_artwork": has_artwork,
    }


def filename_suggestion(path: Path) -> dict[str, str]:
    stem = path.stem
    stem = re.sub(r"\s+", " ", stem).strip()
    parts = re.split(r"\s+[\-–—]\s+", stem, maxsplit=1)
    if len(parts) == 2:
        return {"artist": parts[0].strip(), "title": parts[1].strip()}
    return {"artist": "", "title": stem}


def render_template(template: str, tags: dict[str, str], extension: str) -> str:
    if len(template) > 160:
        raise MediaError("Naming template is too long")
    unknown = re.findall(r"\{([^}]+)\}", template)
    if any(
        token
        not in {"artist", "title", "album", "albumartist", "date", "year", "genre", "track"}
        for token in unknown
    ):
        raise MediaError("Naming template contains an unsupported token")
    values = dict(tags)
    values["year"] = (tags.get("date") or "")[:4]
    values["track"] = tags.get("tracknumber", "")
    rendered = TEMPLATE_TOKEN.sub(lambda match: values.get(match.group(1), ""), template)
    rendered = re.sub(r"\s+", " ", rendered).strip(" .-")
    return sanitize_filename(rendered, extension)


def write_metadata_copy(source: Path, temporary: Path, tags: dict[str, str]) -> None:
    shutil.copy2(source, temporary)
    try:
        audio = mutagen.File(temporary, easy=True)
        if audio is None:
            raise MediaError("Unsupported tag format")
        if audio.tags is None:
            audio.add_tags()
        for field in TAG_FIELDS:
            value = str(tags.get(field, "")).strip()
            try:
                if value:
                    audio[field] = [value]
                elif field in audio:
                    del audio[field]
            except KeyError:
                if value:
                    raise MediaError(f"{field} is not supported by this file format")
        audio.save()
        inspect_media(temporary)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def extract_artwork(path: Path) -> tuple[bytes, str] | None:
    full = mutagen.File(path, easy=False)
    if isinstance(full, MP4) and full.tags and full.tags.get("covr"):
        cover = full.tags["covr"][0]
        mime = (
            "image/png"
            if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG
            else "image/jpeg"
        )
        return bytes(cover), mime
    if isinstance(full, FLAC) and full.pictures:
        picture = full.pictures[0]
        return picture.data, picture.mime or "image/jpeg"
    if full and full.tags:
        for frame in full.tags.values():
            if isinstance(frame, APIC):
                return frame.data, frame.mime or "image/jpeg"
    return None


def write_artwork_copy(source: Path, temporary: Path, image: bytes, mime: str) -> None:
    if len(image) > 10 * 1024 * 1024 or mime not in {"image/jpeg", "image/png"}:
        raise MediaError("Artwork must be a JPEG or PNG smaller than 10 MB")
    try:
        from io import BytesIO

        with Image.open(BytesIO(image)) as decoded:
            decoded.verify()
            if decoded.width > 8000 or decoded.height > 8000:
                raise MediaError("Artwork dimensions are too large")
            actual_mime = Image.MIME.get(decoded.format or "")
            if actual_mime != mime:
                raise MediaError("Artwork content does not match its media type")
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError("Artwork is not a valid JPEG or PNG") from exc
    shutil.copy2(source, temporary)
    suffix = source.suffix.lower()
    if suffix == ".mp3":
        tags = ID3(temporary)
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image))
        tags.save(temporary)
    elif suffix in {".m4a", ".mp4"}:
        audio = MP4(temporary)
        if audio.tags is None:
            audio.add_tags()
        image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio.tags["covr"] = [MP4Cover(image, imageformat=image_format)]
        audio.save()
    elif suffix == ".flac":
        from mutagen.flac import Picture

        audio = FLAC(temporary)
        audio.clear_pictures()
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.desc = "Cover"
        picture.data = image
        audio.add_picture(picture)
        audio.save()
    else:
        temporary.unlink(missing_ok=True)
        raise MediaError("Artwork editing is supported for MP3, M4A, MP4 and FLAC")
    inspect_media(temporary)
