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


def track_file(number: int, title: str, ext: str, disc: int = 1, discs: int = 1) -> str:
    """The title is followed by the extension, so a trailing dot or space in it is legal and kept.

    Disc rule: only an album with more than one disc (`discs` > 1) gets a disc prefix, `D{disc}-{NN} - {Title}.ext`,
    because disc 1 and disc 2 both have a track 01. A single-disc album keeps `NN - Title.ext` exactly, so nothing
    already in the library is renamed."""
    prefix = f"D{disc}-" if discs > 1 else ""
    return f"{prefix}{number:02d} - {clean(title)}{ext if ext.startswith('.') else '.' + ext}"
