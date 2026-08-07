"""Filename/path sanitization + Jellyfin layout for MusicFetch.

Library convention (Adi's): {Artist}/{Album}/{Song}.flac
Multi-artist: kept as "Artist A; Artist B" (the ';' separator is the
established convention across the library — Jellyfin handles it).
"""
from __future__ import annotations

import re

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MULTI_SPACE = re.compile(r"\s{2,}")


def sanitize_component(name: str) -> str:
    """Make a string safe as one path component, preserving case + ';'."""
    if not name:
        return "Unknown"
    s = _BAD.sub("_", name)
    s = _MULTI_SPACE.sub(" ", s)
    s = s.strip(" .")
    return s or "Unknown"


def target_path(meta: dict, output_root: str, ext: str = ".flac",
                dir_artist: str | None = None) -> str:
    """Build {root}/{artist}/{album}/{title}{ext}, avoiding collisions.

    dir_artist overrides the *directory* artist only (used for album
    downloads: all tracks of an album live under the album artist's folder
    while each track keeps its own artist tag).
    """
    artist = sanitize_component(dir_artist or meta.get("artist") or "Unknown Artist")
    album = sanitize_component(meta.get("album") or meta.get("unknown_album") or "Unknown Album")
    title = sanitize_component(meta.get("title") or "Unknown Title")
    ext = ext.lower() if ext.startswith(".") else f".{ext.lower()}"

    base = f"{output_root}/{artist}/{album}"
    candidate = f"{base}/{title}{ext}"
    n = 1
    while _exists(candidate):
        n += 1
        candidate = f"{base}/{title} ({n}){ext}"
    return candidate


def _exists(p: str) -> bool:
    import os

    return os.path.exists(p)
