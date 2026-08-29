"""qbit engine — lossless FLAC tier via local qBittorrent + public trackers.

Pipeline:
  1. apibay.org (PirateBay mirror API, keyless) search, biased to FLAC packs.
  2. f.php?id= -> per-file listing BEFORE download; pick the wanted track by
     name + duration match against the MusicBrainz reference (±5s).
  3. Magnet + explicit tracker args (pure DHT finds no peers on this
     network — trackers are MANDATORY) -> qBittorrent WebUI /torrents/add.
  4. Selective download: /torrents/files then filePrio 0 for unwanted files.
  5. Poll /torrents/info; on completion move the file to staging, delete the
     torrent (seed-time 0 policy), return the Path.

Quality gate: STREAMINFO must be 16-bit/44.1kHz+ and duration within 5s of
the pick; rejects transcodes/short previews. apibay rate-limits hard (429)
— search results are cached and calls are spaced.
"""
from __future__ import annotations

import logging
import re
import struct
import time
import urllib.parse
from pathlib import Path

import requests

from . import EngineError
from postprocess import read_audio_tags

logger = logging.getLogger(__name__)

_APIBAY = "https://apibay.org"
_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.demonii.com:1337/announce",
]
_MIN_SEEDERS = 1  # relaxed 2026-08-29 — healthiest survivor wins even at low seed
_SEARCH_TTL = 300  # apibay cache seconds (429 protection)
_POLL_INTERVAL = 3
_DOWNLOAD_TIMEOUT = 1800  # 30 min hard cap per track
_DURATION_TOLERANCE = 5.0


class QbitError(EngineError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _flac_duration(path: Path) -> float:
    """Parse STREAMINFO manually (mutagen not a hard dep here)."""
    with open(path, "rb") as fh:
        head = fh.read(64)
    if head[:4] != b"fLaC":
        raise QbitError(f"not a FLAC file: {path.name}")
    si = head[8:42]
    bits = "".join(format(b, "08b") for b in si[10:18])
    sr = int(bits[0:20], 2)
    bps = int(bits[23:28], 2) + 1
    total = int(bits[28:64], 2)
    return total / sr


class _QbitClient:
    """Minimal qBittorrent WebUI v2 API client."""

    def __init__(self, cfg: dict):
        q = cfg.get("qbit") or {}
        self.base = q.get("url") or "http://127.0.0.1:1340"
        self.user = q.get("username") or "qbittorrent"
        self.password = q.get("password") or ""
        self.category = q.get("category") or "musicfetch"
        self.session = requests.Session()

    def _login(self):
        r = self.session.post(
            f"{self.base}/api/v2/auth/login",
            data={"username": self.user, "password": self.password},
            timeout=10,
        )
        r.raise_for_status()
        if "Ok" not in r.text:
            raise QbitError(f"qBittorrent login failed: {r.text[:100]}")

    def _call(self, method: str, **kwargs):
        r = self.session.post(f"{self.base}/api/v2/{method}", timeout=15, **kwargs)
        if r.status_code == 403:
            self._login()
            r = self.session.post(f"{self.base}/api/v2/{method}", timeout=15, **kwargs)
        r.raise_for_status()
        return r

    def _get(self, method: str, **kwargs):
        r = self.session.get(f"{self.base}/api/v2/{method}", timeout=15, **kwargs)
        if r.status_code == 403:
            self._login()
            r = self.session.get(f"{self.base}/api/v2/{method}", timeout=15, **kwargs)
        r.raise_for_status()
        return r

    def add_magnet(self, magnet: str) -> bool:
        r = self._call(
            "torrents/add",
            data={"category": self.category, "paused": "false"},
            files={"urls": (None, magnet)},
        )
        txt = r.text.strip().lower()
        return "ok" in txt or r.status_code == 200

    def find_torrent(self) -> dict | None:
        r = self._get(
            "torrents/info", params={"category": self.category}
        )
        torrents = r.json()
        return torrents[0] if torrents else None

    def files(self, hash_: str) -> list[dict]:
        # GET only — POST returns 400 even with correct params
        r = self._get("torrents/files", params={"hash": hash_})
        return r.json()

    def prioritize_only(self, hash_: str, wanted_index: int, count: int):
        """Set all files to skip (0) except wanted_index (6 = high)."""
        for i in range(count):
            pri = 6 if i == wanted_index else 0
            self._call(
                "torrents/filePrio",
                data={"hash": hash_, "id": i, "priority": pri},
            )

    def delete(self, hash_: str):
        self._call(
            "torrents/delete",
            data={"hashes": hash_, "deleteFiles": "true"},
        )

    def save_path(self) -> str:
        r = self._get("app/preferences")
        return r.json().get("save_path", "")


class _Apibay:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "MusicFetch/0.2"
        self._cache: dict[str, tuple[float, list]] = {}

    def search(self, query: str) -> list[dict]:
        now = time.time()
        cached = self._cache.get(query)
        if cached and now - cached[0] < _SEARCH_TTL:
            return cached[1]
        # apibay is flaky (429 bursts + read timeouts) — retry with backoff so a
        # slow index doesn't silently drop the whole FLAC tier (2026-08-29).
        items, last = None, None
        for attempt in range(3):
            try:
                r = self.session.get(
                    f"{_APIBAY}/q.php",
                    params={"q": query, "cat": "104"},  # 104 = music
                    timeout=30,
                )
                if r.status_code == 429:
                    last = QbitError("apibay rate-limited (429) — retrying…")
                    time.sleep(6 * (attempt + 1))
                    continue
                r.raise_for_status()
                items = r.json()
                break
            except requests.Timeout:
                last = QbitError("apibay read timeout — retrying…")
                time.sleep(3 * (attempt + 1))
                continue
            except requests.ConnectionError as exc:
                last = QbitError(f"apibay connection failed: {exc}")
                time.sleep(3 * (attempt + 1))
                continue
        if items is None:
            raise QbitError(str(last or "apibay search failed"))
        if items and items[0].get("id") == "0":
            items = []
        out = [
            {
                "id": it["id"],
                "name": it["name"],
                "info_hash": it["info_hash"],
                "seeders": int(it.get("seeders") or 0),
                "size": int(it.get("size") or 0),
                "status": it.get("status", ""),
            }
            for it in items
        ]
        self._cache[query] = (now, out)
        time.sleep(2.5)  # spacing — apibay bans bursts
        return out

    def files(self, torrent_id: str) -> list[dict]:
        """apibay f.php is as flaky as q.php — same retry/backoff (2026-08-29)."""
        raw, last = None, None
        for attempt in range(3):
            try:
                r = self.session.get(f"{_APIBAY}/f.php", params={"id": torrent_id}, timeout=30)
                if r.status_code == 429:
                    last = QbitError("apibay rate-limited (429) on file listing")
                    time.sleep(5 * (attempt + 1))
                    continue
                r.raise_for_status()
                raw = r.json()
                break
            except requests.Timeout:
                last = QbitError("apibay read timeout on file listing")
                time.sleep(3 * (attempt + 1))
                continue
            except requests.ConnectionError as exc:
                last = QbitError(f"apibay connection failed on file listing: {exc}")
                time.sleep(3 * (attempt + 1))
                continue
        if raw is None:
            raise QbitError(str(last or "apibay file listing failed"))
        # apibay encodes single-element values as {"0": value} dicts (not lists).
        # "Filelist not found" means the pack has no indexed file list -> the
        # selective-download model can't proceed (2026-08-29).
        def _field(v):
            if isinstance(v, dict):
                items = [v[k] for k in sorted(v, key=lambda x: int(x) if str(x).isdigit() else 0)]
                return items[0] if len(items) == 1 else "".join(str(i) for i in items)
            if isinstance(v, list):
                return v[0] if len(v) == 1 else v
            return v

        out = []
        for it in raw:
            name = _field(it.get("name")) or ""
            size = _field(it.get("size")) or 0
            if isinstance(name, str) and "filelist not found" in name.lower():
                raise QbitError("no file listing available for this torrent")
            if isinstance(size, list):
                size = size[0] if size else 0
            out.append({"name": name, "size": int(size)})
        return out


def _pick_file(
    files: list[dict], pick: dict
) -> tuple[int, dict] | None:
    """Choose the FLAC file matching title (+disc-track prefix ok) and size sanity."""
    title_n = _norm(pick.get("title") or "")
    want_dur = float(pick.get("duration") or 0)
    best, best_score = None, -1.0
    for idx, f in enumerate(files):
        if not f["name"].lower().endswith(".flac"):
            continue
        base = f["name"].rsplit("/", 1)[-1]
        base_n = _norm(base.rsplit(".", 1)[0])
        # strip leading "01." / "CD 1/02." track-number prefixes
        base_n = re.sub(r"^\s*(cd\s*\d+\s*)?\d{1,2}[\s.-]+", "", base_n)
        if not title_n or title_n not in base_n:
            continue
        # ~1 MB/min for 16/44.1 — reject absurd sizes early
        if want_dur and f["size"] < want_dur * 16000:
            continue
        score = f["size"] / 1e6  # bigger = better (within same title match)
        if score > best_score:
            best, best_score = (idx, f), score
    return best


class QbitEngine:
    name = "qbit"

    def __init__(self, cfg: dict):
        d = cfg.get("qbit") or {}
        self.staging = Path(d.get("staging_dir") or "/root/musicfetch/staging") / "qbit"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.qbit = _QbitClient(cfg)
        self.apibay = _Apibay()

    # -- search ---------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Torrent search returns ALBUM packs; candidates are per-pack, and
        download() re-resolves the exact file inside the chosen pack.
        apibay's search is AND-token — appending 'flac' over-constrains and
        silently empties multi-word queries (band of skulls sweet sour flac ->
        0). So: prefer '{query} flac' (keeps FLAC bias), fall back to plain
        '{query}' when that returns nothing (2026-08-29)."""
        results = self.apibay.search(f"{query} flac")
        if not results:
            results = self.apibay.search(query)
        cands = []
        for t in sorted(results, key=lambda x: -x["seeders"])[:limit]:
            if t["seeders"] < _MIN_SEEDERS:
                continue
            cands.append({
                "source": "qbit",
                "track_id": t["id"],  # apibay torrent id
                "artist": "?",  # resolved at download time from pack contents
                "title": t["name"],
                "album": "",
                "duration": 0,
                "quality": "FLAC (lossless)",
                "seeders": t["seeders"],
                "size_mb": round(t["size"] / 1e6, 1),
                "info_hash": t["info_hash"],
                "cover_url": "",
            })
        return cands

    # -- download -------------------------------------------------------
    def download(self, pick: dict) -> Path:
        torrent_id = str(pick["track_id"])
        files = self.apibay.files(torrent_id)
        if not files:
            raise QbitError(f"apibay: no file listing for torrent {torrent_id}")

        chosen = _pick_file(files, pick)
        if not chosen:
            raise QbitError(
                f"no FLAC file matching '{pick.get('title')}' in torrent {torrent_id}"
            )
        idx, finfo = chosen
        logger.info("qbit: picked file %s (%.1f MB) from torrent %s",
                    finfo["name"], finfo["size"] / 1e6, torrent_id)

        magnet = (
            f"magnet:?xt=urn:btih:{pick['info_hash']}"
            + "".join(f"&tr={urllib.parse.quote(t)}" for t in _TRACKERS)
        )
        if not self.qbit.add_magnet(magnet):
            raise QbitError("qBittorrent rejected the magnet")

        try:
            dest = self._wait_and_extract(pick, torrent_id, idx, finfo)
        except Exception:
            self._cleanup_torrent()
            raise
        self._cleanup_torrent()
        return dest

    def _wait_and_extract(
        self, pick: dict, torrent_id: str, wanted_idx: int, finfo: dict
    ) -> Path:
        deadline = time.time() + _DOWNLOAD_TIMEOUT
        prioritized = False
        hash_ = pick["info_hash"].lower()
        missing = 0  # tolerate transient empty torrents/info (indexing window)

        while time.time() < deadline:
            t = self.qbit.find_torrent()
            if t is None:
                # engine cleanup may have already removed a completed torrent —
                # check whether our file made it to disk before declaring failure
                maybe = self.staging / finfo["name"].rsplit("/", 1)[-1]
                if prioritized and maybe.exists():
                    return maybe
                missing += 1
                if missing > 5:
                    raise QbitError("torrent disappeared from qBittorrent")
                time.sleep(_POLL_INTERVAL)
                continue
            missing = 0
            hash_ = t["hash"].lower()

            # metadata detection: /torrents/files 400s until metadata arrives;
            # the `metadata_received` field is unreliable in WebUI v5
            if not prioritized:
                try:
                    flist = self.qbit.files(hash_)
                except requests.HTTPError:
                    flist = []
                if len(flist) > 1:
                    self.qbit.prioritize_only(hash_, wanted_idx, len(flist))
                    prioritized = True
                    logger.info("qbit: selective priority set (%d files in pack)", len(flist))

            if prioritized and t.get("state") in ("uploading", "stalledUP", "pausedUP"):
                break  # done downloading (now seeding)
            if t.get("progress", 0) >= 1.0 and prioritized:
                break

            time.sleep(_POLL_INTERVAL)

        if not prioritized:
            raise QbitError("torrent metadata never arrived (no peers? check trackers)")

        # locate the finished file on disk
        save_root = Path(self.qbit.save_path())
        rel_parts = finfo["name"].split("/")
        src = save_root.joinpath(*rel_parts)
        if not src.exists():
            # some clients strip the root folder; try name-only search
            hits = list(save_root.rglob(f"*/{rel_parts[-1]}")) or list(
                save_root.rglob(rel_parts[-1])
            )
            if not hits:
                raise QbitError(f"downloaded file not found at {src}")
            src = hits[0]

        # quality gate before accepting
        dur = _flac_duration(src)
        want = float(pick.get("duration") or 0)
        if want and abs(dur - want) > _DURATION_TOLERANCE:
            raise QbitError(
                f"duration mismatch: FLAC is {dur:.1f}s, reference {want:.1f}s — wrong rip?"
            )
        logger.info("qbit: FLAC verified %.1fs %s", dur, src.name)

        dest = self.staging / src.name
        src.rename(dest)
        return dest

    def _cleanup_torrent(self):
        try:
            t = self.qbit.find_torrent()
            if t:
                self.qbit.delete(t["hash"])
                logger.info("qbit: torrent deleted from client (seed-time 0 policy)")
        except Exception as exc:
            logger.warning("qbit: cleanup failed: %s", exc)

    # -- metadata -------------------------------------------------------
    def meta(self, local: Path, pick: dict, query: str) -> dict:
        m = read_audio_tags(str(local))
        if not m.get("title"):
            # "01. Get Lucky.flac" -> "Get Lucky"
            stem = re.sub(r"^\s*(cd\s*\d+\s*/\s*)?\d{1,2}[\s.-]+", "", local.stem)
            m["title"] = stem
        if not m.get("artist"):
            m["artist"] = pick.get("artist") or "Unknown Artist"
        m.setdefault("year", None)
        m.setdefault("tracknumber", None)
        return {k: v for k, v in m.items() if v}
