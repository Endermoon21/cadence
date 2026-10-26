"""The one place that knows the library layout: `Artist/Album/NN - Title.ext` (see DESIGN.md section 5).

Only characters that exFAT/macOS cannot hold are touched (`/ : * ? " < > |`, trailing dots and spaces on a
folder name); the replacements follow what the existing library already shows: `/` -> `+`, `:` -> `-`
(`: ` -> ` - `), `*` -> `-`, and `? " < > |` are dropped."""
from __future__ import annotations

import re

_DROP = re.compile(r'[?"<>|]')


def clean(name: str) -> str:
    name = name.replace(": ", " - ").replace(":", "-").replace("/", "+").replace("*", "-")
    return _DROP.sub("", name)


def album_dir(artist: str, album: str) -> str:
    return "/".join(clean(part).rstrip(". ") for part in (artist, album))


def track_file(number: int, title: str, ext: str) -> str:
    """The title is followed by the extension, so a trailing dot or space in it is legal and kept."""
    return f"{number:02d} - {clean(title)}{ext if ext.startswith('.') else '.' + ext}"
