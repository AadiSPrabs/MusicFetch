"""JioSaavn engine — public API, no auth, 320kbps AAC full-length tracks.

Recipe (works from CGNAT home NAT, no account, no VPN):
  1. search.getResults (official API) -> candidates (id, meta, 320kbps flag).
  2. song.getDetails by id -> encrypted_media_url.
  3. DES-ECB decrypt (key "38346591") -> CDN URL like ..._{96,160,320}.mp4.
  4. The anonymous API serves 96kbps; the CDN serves 320/160 to anyone —
     swap the quality suffix and download full-length AAC.

Free tier quality: 320kbps AAC (~323kbps measured). Never lossless.
"""
from __future__ import annotations

import base64
import html
import logging
import re
from pathlib import Path

import requests
from Cryptodome.Cipher import DES
from Cryptodome.Util.Padding import unpad

from . import EngineError
from postprocess import read_audio_tags

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_API = "https://www.jiosaavn.com/api.php"
_DES_KEY = b"38346591"
_QUALITIES = (320, 160, 96)  # preferred order; 128 doesn't exist on their CDN


class JioSaavnError(EngineError):
    pass


def _decrypt_url(enc_url: str) -> str:
    try:
        dec = base64.b64decode(enc_url.strip())
        plain = unpad(DES.new(_DES_KEY, DES.MODE_ECB).decrypt(dec), 8)
        return plain.decode("utf-8", "ignore").strip().replace("http://", "https://")
    except Exception as exc:
        raise JioSaavnError(f"could not decrypt media URL: {exc}") from exc


def _pick_quality_url(url96: str, want: int) -> str:
    """Swap the CDN quality suffix; fall back down the chain if 404."""
    m = re.search(r"_(\d+)\.mp4$", url96)
    if not m:
        return url96
    base = url96[: m.start(1)]
    for q in _QUALITIES:
        cand = f"{base}{q}.mp4"
        if q == want:
            return cand  # engine-level fallback handled by caller
    return url96


class JioSaavnEngine:
    name = "jiosaavn"

    def __init__(self, cfg: dict):
        d = cfg.get("jiosaavn") or {}
        self.staging = Path(d.get("staging_dir") or "/root/musicfetch/staging") / "jiosaavn"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = _UA

    # -- API ------------------------------------------------------------
    def _call(self, call: str, **params) -> dict:
        params.setdefault("_format", "json")
        params.setdefault("_marker", 0)
        params.setdefault("api_version", 4)
        resp = self.session.get(_API, params={"__call": call, **params}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise JioSaavnError(f"jiosaavn {call}: {data['error']}")
        return data

    # -- search ---------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[dict]:
        data = self._call("search.getResults", q=query)
        items = data.get("results", []) if isinstance(data, dict) else []
        cands = []
        for it in items[:limit]:
            mi = it.get("more_info") or {}
            artists = [
                a.get("name") for a in (mi.get("artistMap") or {}).get("primary_artists", [])
                if a.get("name")
            ]
            artist = "; ".join(artists) or (mi.get("song") or "").split(" - ")[0].strip() or "?"
            title = html.unescape(it.get("title") or "?")
            if not artists and " - " in (mi.get("song") or ""):
                title = (mi.get("song") or "").split(" - ", 1)[1].strip()
            image = it.get("image") or ""
            cover_url = image.replace("-150x150.jpg", "-500x500.jpg") if image else ""
            cands.append({
                "source": "jiosaavn",
                "track_id": str(it.get("id", "")),
                "artist": artist,
                "title": title,
                "album": html.unescape(mi.get("album") or ""),
                "duration": int(mi.get("duration") or it.get("duration") or 0),
                "quality": "AAC 320" if mi.get("320kbps") == "true" else "AAC 160",
                "plays": it.get("play_count", "0"),
                "year": it.get("year"),
                "cover_url": cover_url,
            })
        return [c for c in cands if c["track_id"]]

    # -- download -------------------------------------------------------
    def download(self, pick: dict) -> Path:
        tid = str(pick["track_id"])
        data = self._call("song.getDetails", pids=tid)
        # response is keyed by song id: {"6uEI9gj0": {song}}
        song = data.get(tid) or {}
        if not song:
            # some ids come back under a nested 'songs' list
            songs = data.get("songs") or []
            song = songs[0] if songs else {}
        if not song:
            raise JioSaavnError(f"jiosaavn: no details for {tid}")
        mi = song.get("more_info") or {}
        enc = mi.get("encrypted_media_url") or mi.get("encrypted_media_path")
        if not enc:
            raise JioSaavnError(f"jiosaavn: no media URL for {tid}")
        url96 = _decrypt_url(enc)

        want = 320 if mi.get("320kbps") == "true" else 160
        for q in (want, 160, 96):
            url = _pick_quality_url(url96, q)
            try:
                with self.session.get(url, stream=True, timeout=180) as resp:
                    if resp.status_code != 200:
                        logger.info("jiosaavn: fmt %s -> HTTP %s, trying lower", q, resp.status_code)
                        continue
                    dest = self.staging / f"track_{tid}.m4a"
                    with open(dest, "wb") as fh:
                        for chunk in resp.iter_content(65536):
                            if chunk:
                                fh.write(chunk)
                    if dest.stat().st_size < 65536:
                        raise JioSaavnError("tiny download — likely an error page")
                    logger.info("jiosaavn: downloaded %s (%d bytes, q%d)", tid, dest.stat().st_size, q)
                    return dest
            except (requests.RequestException, OSError) as exc:
                logger.info("jiosaavn: fmt %s failed: %s", q, exc)
        raise JioSaavnError(f"jiosaavn: all qualities failed for {tid}")

    # -- metadata -------------------------------------------------------
    def meta(self, local: Path, pick: dict, query: str) -> dict:
        m = read_audio_tags(str(local))
        if not m.get("title"):
            m["title"] = pick.get("title") or local.stem
        if not m.get("artist"):
            m["artist"] = pick.get("artist") or "Unknown Artist"
        if not m.get("album"):
            m["album"] = pick.get("album") or ""
        m.setdefault("year", pick.get("year"))
        m.setdefault("tracknumber", None)
        if pick.get("cover_url"):
            m["cover_url"] = pick["cover_url"]
        return {k: v for k, v in m.items() if v}
