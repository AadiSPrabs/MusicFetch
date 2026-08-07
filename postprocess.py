"""Post-processing: MusicBrainz metadata, LRCLIB lyrics, cover art, mutagen tags.

All external APIs are free + keyless:
  * MusicBrainz  — /ws/2/recording search (requires a UA; 1 req/s)
  * Cover Art Archive — /release/{mbid}/front-500
  * LRCLIB       — /api/search?track_name=&artist_name=  (plain + synced)
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import time

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, USLT
from mutagen.mp3 import MP3

MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org/release"
LRCLIB = "https://lrclib.net/api"

GENRES = ("Rock", "Pop", "Hip-Hop", "Jazz", "Electronic", "Classical", "Metal",
          "Indie", "R&B", "Folk", "Blues", "Country", "Reggae", "Ambient")


def _sniff_image(data: bytes) -> str | None:
    """Return 'jpeg' | 'png' | 'webp' | None from magic bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _webp_to_jpeg(data: bytes) -> bytes | None:
    """Convert WebP to JPEG via ffmpeg (stdin/stdout pipes, no temp files)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0", "-f", "mjpeg", "pipe:1"],
            input=data, capture_output=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def _normalize_cover(cover: bytes | None) -> tuple[bytes | None, str]:
    """Return (bytes, mime) with WebP converted to JPEG; PNG kept as PNG.

    Covers arrive as WebP from YouTube thumbnails but MP4 'covr' atoms must
    carry a real image format — embedding WebP bytes labeled JPEG produces
    art that players cannot decode ("no thumbnail").
    """
    if not cover:
        return None, ""
    kind = _sniff_image(cover)
    if kind == "jpeg":
        return cover, "image/jpeg"
    if kind == "png":
        return cover, "image/png"
    if kind == "webp":
        jpg = _webp_to_jpeg(cover)
        return (jpg, "image/jpeg") if jpg else (None, "")
    return cover, "image/jpeg"  # unknown format — best effort


def read_audio_tags(path: str) -> dict:
    """Extract title/artist/album/album_artist/tracknumber/date from FLAC or MP3."""
    import os

    out: dict = {}
    try:
        if path.lower().endswith((".m4a", ".mp4", ".m4b")):
            return read_m4a_tags(path)
        if path.lower().endswith(".mp3"):
            audio = MP3(path)
            tags = audio.tags
            if tags is not None:
                def t(key):
                    frames = tags.getall(key)
                    return str(frames[0].text[0]) if frames and frames[0].text else None
                out = {
                    "title": t("TIT2"), "artist": t("TPE1"), "album": t("TALB"),
                    "album_artist": t("TPE2"), "tracknumber": t("TRCK"), "date": t("TDRC"),
                }
        else:
            audio = FLAC(path)
            def g(key):
                v = audio.get(key)
                return v[0] if v else None
            out = {
                "title": g("title"), "artist": g("artist"), "album": g("album"),
                "album_artist": g("albumartist"), "tracknumber": g("tracknumber"),
                "date": g("date"),
            }
    except Exception as exc:  # unreadable/corrupt file — fall through
        logger.warning("read_audio_tags: could not read %s: %s", path, exc)
    return {k: v for k, v in out.items() if v}


def read_m4a_tags(path: str) -> dict:
    """Extract title/artist/album/album_artist/tracknumber/date from M4A/MP4."""
    from mutagen.mp4 import MP4

    out: dict = {}
    try:
        audio = MP4(path)
        if audio.tags is None:
            return out

        def t(key):
            v = audio.tags.get(key)
            return str(v[0]) if v and v[0] else None

        trkn = audio.tags.get("trkn")
        track = str(trkn[0][0]) if trkn and trkn[0] else None
        out = {
            "title": t("\xa9nam"), "artist": t("\xa9ART"), "album": t("\xa9alb"),
            "album_artist": t("aART"), "tracknumber": track, "date": t("\xa9day"),
        }
    except Exception as exc:
        logger.warning("read_m4a_tags: could not read %s: %s", path, exc)
    return {k: v for k, v in out.items() if v}


class PostProcessor:
    def __init__(self, cfg: dict, ua: str | None = None):
        self.ua = ua or cfg.get("postprocess", {}).get(
            "musicbrainz_ua", "MusicFetch/0.1 (local service)"
        )
        self.do_mb = cfg.get("postprocess", {}).get("musicbrainz", True)
        self.do_lyrics = cfg.get("postprocess", {}).get("lyrics", True)
        self.do_cover = cfg.get("postprocess", {}).get("cover", True)

    # -- musicbrainz ----------------------------------------------------
    def musicbrainz(self, artist: str, title: str) -> dict | None:
        if not self.do_mb:
            return None
        query = f'artist:"{artist}" AND recording:"{title}"'.replace('"', '\\"')
        r = requests.get(
            f"{MB}/recording",
            params={"query": query, "fmt": "json", "limit": 5},
            headers={"User-Agent": self.ua},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        recs = (r.json().get("recordings") or [])[:5]
        for rec in recs:
            rels = rec.get("releases") or []
            if not rels:
                continue
            rel = rels[0]
            artist_credit = " & ".join(
                (c.get("name") or c.get("artist", {}).get("name", ""))
                for c in (rec.get("artist-credit") or [])
                if isinstance(c, dict)
            ) or artist
            track = None
            for m in rel.get("media") or []:
                for t in m.get("tracks") or []:
                    if (t.get("title") or "").lower() == (rec.get("title") or "").lower():
                        track = t.get("number")
                        break
                if track:
                    break
            return {
                "title": rec.get("title") or title,
                "artist": artist_credit,
                "album": rel.get("title"),
                "album_artist": " & ".join(
                    (c.get("name") or "")
                    for c in (rel.get("artist-credit") or [])
                    if isinstance(c, dict)
                ) or artist_credit,
                "tracknumber": str(track) if track else None,
                "date": (rel.get("date") or "")[:4] or None,
                "mbid": rec.get("id"),
                "release_mbid": rel.get("id"),
            }
        return None

    def _album_genre(self, rel_mbid: str) -> str | None:
        # genre via release-group is extra calls; keep simple: skip for v1
        return None

    # -- cover ----------------------------------------------------------
    def cover(self, release_mbid: str | None, artist: str, album: str) -> bytes | None:
        if not self.do_cover or not release_mbid:
            return None
        try:
            r = requests.get(f"{CAA}/{release_mbid}/front-500", timeout=20)
            if r.status_code == 200:
                return r.content
        except requests.RequestException:
            pass
        return None

    # -- lyrics ---------------------------------------------------------
    def lyrics(self, artist: str, title: str) -> dict | None:
        if not self.do_lyrics:
            return None
        try:
            r = requests.get(
                f"{LRCLIB}/search",
                params={"artist_name": artist, "track_name": title},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            hits = r.json() or []
            if not hits:
                return None
            hit = hits[0]
            return {
                "synced": hit.get("syncedLyrics") or None,
                "plain": hit.get("plainLyrics") or None,
            }
        except requests.RequestException:
            return None

    # -- apply ----------------------------------------------------------
    def apply(self, path: str, meta: dict, lyrics: dict | None, cover: bytes | None,
              genre: str | None = None) -> None:
        cover, cover_mime = _normalize_cover(cover)
        if path.lower().endswith(".mp3"):
            self._apply_mp3(path, meta, lyrics, cover, genre, cover_mime)
        elif path.lower().endswith((".m4a", ".mp4", ".m4b")):
            self._apply_m4a(path, meta, lyrics, cover, genre, cover_mime)
        else:
            self._apply_flac(path, meta, lyrics, cover, genre, cover_mime)
        # Jellyfin shows lyrics reliably from an .lrc sidecar (embedded
        # \xa9lyr/USLT support is spotty across versions).
        if lyrics:
            text = lyrics.get("synced") or lyrics.get("plain")
            if text:
                sidecar = os.path.splitext(path)[0] + ".lrc"
                with open(sidecar, "w", encoding="utf-8") as fh:
                    fh.write(text)

    def _apply_flac(self, path: str, meta: dict, lyrics: dict | None, cover: bytes | None,
                    genre: str | None = None, cover_mime: str = "image/jpeg") -> None:
        audio = FLAC(path)
        tags = {
            "title": meta.get("title"),
            "artist": meta.get("artist"),
            "album": meta.get("album"),
            "albumartist": meta.get("album_artist"),
            "tracknumber": meta.get("tracknumber"),
            "date": meta.get("date"),
        }
        for key, val in tags.items():
            if val:
                audio[key] = val
        if genre:
            audio["genre"] = genre
        if lyrics:
            if lyrics.get("synced"):
                audio["lyrics"] = lyrics["synced"]
            elif lyrics.get("plain"):
                audio["lyrics"] = lyrics["plain"]
        if cover:
            pic = Picture()
            pic.type = 3  # front cover
            pic.mime = cover_mime
            pic.data = cover
            pic.desc = "cover"
            audio.add_picture(pic)
        audio.save()

    def _apply_mp3(self, path: str, meta: dict, lyrics: dict | None, cover: bytes | None,
                   genre: str | None = None, cover_mime: str = "image/jpeg") -> None:
        """ID3 tagging for MP3 files (same truthy-only overwrite semantics)."""
        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        _TEXT = {"TIT2": TIT2, "TPE1": TPE1, "TALB": TALB, "TPE2": TPE2,
                 "TRCK": TRCK, "TDRC": TDRC, "TCON": TCON}
        values = {
            "TIT2": meta.get("title"), "TPE1": meta.get("artist"),
            "TALB": meta.get("album"), "TPE2": meta.get("album_artist"),
            "TRCK": meta.get("tracknumber"), "TDRC": meta.get("date"),
            "TCON": genre,
        }
        for key, val in values.items():
            if not val:
                continue
            tags.delall(key)
            tags.add(_TEXT[key](encoding=3, text=[str(val)]))

        if lyrics:
            text = lyrics.get("synced") or lyrics.get("plain")
            if text:
                tags.delall("USLT")
                tags.add(USLT(encoding=3, lang="eng", desc="", text=text))

        if cover:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=cover_mime, type=3, desc="cover", data=cover))
        audio.save()

    def _apply_m4a(self, path: str, meta: dict, lyrics: dict | None, cover: bytes | None,
                   genre: str | None = None, cover_mime: str = "image/jpeg") -> None:
        """MP4/M4A tagging (same truthy-only overwrite semantics)."""
        from mutagen.mp4 import MP4, MP4Cover

        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        def put(atom, val):
            if not val:
                return
            tags[atom] = [str(val)]

        put("\xa9nam", meta.get("title"))
        put("\xa9ART", meta.get("artist"))
        put("\xa9alb", meta.get("album"))
        put("aART", meta.get("album_artist"))
        put("\xa9day", meta.get("date") or meta.get("year"))
        put("\xa9gen", genre)
        trkn = meta.get("tracknumber")
        if trkn:
            try:
                num = int(str(trkn).split("/")[0])
                tags["trkn"] = [(num, 0)]
            except ValueError:
                pass

        if lyrics:
            text = lyrics.get("synced") or lyrics.get("plain")
            if text:
                tags["\xa9lyr"] = [text]

        if cover:
            fmt = MP4Cover.FORMAT_PNG if cover_mime == "image/png" else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(cover, imageformat=fmt)]
        audio.save()

    def write_folder_jpg(self, album_dir: str, cover: bytes | None) -> None:
        if not cover:
            return
        import os

        cover, _ = _normalize_cover(cover)
        if not cover:
            return
        os.makedirs(album_dir, exist_ok=True)
        with open(f"{album_dir}/folder.jpg", "wb") as fh:
            fh.write(cover)
