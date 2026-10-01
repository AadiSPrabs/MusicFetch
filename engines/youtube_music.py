"""YouTube Music engine — yt-dlp against music.youtube.com's song catalog.

Fallback engine: covers everything JioSaavn misses (Japanese, niche,
regional). Quality ceiling is ~128kbps AAC (fmt 140) / ~140kbps opus on
free accounts — hence secondary in engine.order.

Search: flat extract of https://music.youtube.com/search?q=... returns
catalog entries (art tracks = clean audio, no music videos). Channel and
playlist entries are filtered out; remaining video ids get a full metadata
pass (track/artist/album straight from the YT Music page).
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from urllib.parse import quote

import yt_dlp

from . import EngineError
from postprocess import read_audio_tags

logger = logging.getLogger(__name__)

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _is_video(entry: dict) -> bool:
    eid = entry.get("id") or ""
    return bool(_VIDEO_ID.match(eid)) and not eid.startswith(("UC", "VL", "PL"))


class YoutubeMusicError(EngineError):
    pass


class YoutubeMusicEngine:
    name = "youtube_music"

    def __init__(self, cfg: dict):
        d = cfg.get("youtube_music") or {}
        self.staging = Path(d.get("staging_dir") or "/root/musicfetch/staging") / "ytmusic"
        self.staging.mkdir(parents=True, exist_ok=True)

    # -- helpers --------------------------------------------------------
    def _flat(self, url: str) -> list[dict]:
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                "skip_download": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return [e for e in (info.get("entries") or []) if isinstance(e, dict)]

    def _meta(self, vid: str) -> dict | None:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "noplaylist": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}",
                                        download=False)
        except Exception as exc:
            logger.warning("ytmusic: metadata for %s failed: %s", vid, exc)
            return None
        if not info:
            return None
        title = info.get("track") or info.get("title")
        artist = info.get("artist") or info.get("channel") or ""
        if not title or not artist:
            return None
        thumbs = info.get("thumbnails") or []
        # Deterministic JPEG from the video id (i.ytimg.com/vi/) instead of
        # the WebP thumbnail — WebP can't be embedded in m4a covr atoms.
        thumb = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg" if vid else (
            thumbs[-1].get("url") if thumbs else None)
        return {
            "source": self.name,
            "track_id": vid,
            "artist": artist,
            "title": title,
            "album": info.get("album") or "",
            "duration": int(info.get("duration") or 0),
            "quality": "AAC ~128",
            "plays": "",
            "year": None,
            "cover_url": thumb or "",
        }

    def video_pick(self, vid: str) -> dict:
        """Full metadata for a single video id — used by the URL resolver
        (pasted youtube/ytmusic single links). Falls back to a minimal pick
        when the metadata pass fails so the download can still proceed."""
        return self._meta(vid) or {
            "source": self.name, "track_id": vid, "artist": "", "title": "",
            "album": "", "duration": 0, "quality": "AAC ~128", "plays": "",
            "year": None, "cover_url": "",
        }

    # -- interface ------------------------------------------------------
    def search(self, query: str, limit: int = 8) -> list[dict]:
        # sp=EgWKAQIIAWoKEAoQAxAEEAkQBQ== — YT Music "Songs" filter: biases
        # results toward clean art tracks instead of videos/lives/covers.
        url = (f"https://music.youtube.com/search?q={quote(query)}"
               "&sp=EgWKAQIIAWoKEAoQAxAEEAkQBQ%3D%3D")
        entries = [e for e in self._flat(url) if _is_video(e)]
        cands = []
        for e in entries[:limit]:
            m = self._meta(e["id"])
            if m:
                cands.append(m)
        return cands

    _CLIENTS = (None, "android", "web_embedded")  # player clients tried in order

    def download(self, pick: dict) -> Path:
        vid = str(pick["track_id"])
        dest = self.staging / f"track_{vid}.m4a"
        first_err: Exception | None = None
        for client in self._CLIENTS:
            opts = {
                "quiet": True, "no_warnings": True, "noprogress": True,
                "noplaylist": True, "retries": 3,
                "format": "140",  # m4a / AAC-LC ~128kbps — keeps the pipeline M4A
                "outtmpl": str(dest),
                # YouTube needs a JS runtime + the EJS challenge-solver distribution
                # to solve the signature / n-challenge. Without them extraction still
                # "succeeds" but hands back URLs that 403 on the data fetch — the
                # "unable to download video data: HTTP Error 403" failure. node is
                # installed here (deno is the other supported runtime).
                "js_runtimes": {"node": {"path": shutil.which("node")}},
                "remote_components": ["ejs:github"],
            }
            if client:
                opts["extractor_args"] = {"youtube": {"player_client": [client]}}
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([f"https://www.youtube.com/watch?v={vid}"])
                if dest.exists():
                    break
            except Exception as exc:
                if first_err is None:
                    first_err = exc
                logger.warning("ytmusic: %s client failed for %s: %s",
                               client or "default", vid, exc)
                for p in (dest, Path(str(dest) + ".part")):
                    p.unlink(missing_ok=True)
        if not dest.exists():
            raise YoutubeMusicError(
                f"ytmusic: download failed for {vid}: {first_err}") from first_err
        logger.info("ytmusic: downloaded %s (%d bytes)", vid, dest.stat().st_size)
        return dest

    def meta(self, local: Path, pick: dict, query: str) -> dict:
        m = read_audio_tags(str(local))
        if not m.get("title"):
            m["title"] = pick.get("title") or local.stem
        if not m.get("artist"):
            m["artist"] = pick.get("artist") or "Unknown Artist"
        if not m.get("album"):
            m["album"] = pick.get("album") or ""
        m.setdefault("year", pick.get("year"))
        if pick.get("cover_url"):
            m["cover_url"] = pick["cover_url"]
        return {k: v for k, v in m.items() if v}
