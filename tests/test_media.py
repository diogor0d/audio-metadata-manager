from __future__ import annotations

from pathlib import Path

import pytest

from liner.media import MediaError, filename_suggestion, render_template, sanitize_filename


def test_filename_sanitization_handles_windows_names() -> None:
    assert sanitize_filename("Artist: Title?.mp3") == "Artist_ Title_.mp3"
    with pytest.raises(MediaError):
        sanitize_filename("CON.mp3")


def test_naming_template_is_declarative() -> None:
    rendered = render_template(
        "{artist} - {title}",
        {"artist": "Artist", "title": "Title", "date": "2026", "tracknumber": "1"},
        ".mp3",
    )
    assert rendered == "Artist - Title.mp3"
    with pytest.raises(MediaError):
        render_template("{__import__('os')}", {}, ".mp3")


def test_filename_suggestion_does_not_guess_which_text_is_junk() -> None:
    suggestion = filename_suggestion(
        Path("Don Toliver - I Don't Rock Adidas (unreleased) [mkVjOk8AJuc].mp3")
    )
    assert suggestion["artist"] == "Don Toliver"
    assert suggestion["title"].endswith("[mkVjOk8AJuc]")
