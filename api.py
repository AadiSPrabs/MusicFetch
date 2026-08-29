"""MusicFetch REST API.

  POST /api/search        {query}                    -> {job_id}  (job.result = candidates)
  POST /api/download      {query, pick}              -> {job_id}  (job.result = output)
  POST /api/resolve       {url}                      -> {job_id}  (link -> track pick or playlist track list)
  POST /api/playlist      {url}                      -> {job_id}  (link -> downloads everything)
  GET  /api/job/{id}                                 -> job status
  GET  /api/jobs                                     -> all jobs
  GET  /health

Flow: search job -> caller picks a candidate -> download job with that pick.
Pasted links (YouTube/YouTube Music/Spotify) go through /api/resolve for
inspection, or straight to /api/playlist to download everything. Engines are
jiosaavn (primary, 320kbps AAC) + youtube_music (fallback, ~128kbps AAC) per
config engine.order; a pick's 'source' field routes the download.

Auth: optional — if api.token is set in config.yaml, clients must send
header X-API-Key. Bind is localhost by default.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import urllib.request
from collections import Counter

import yaml
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engines import EngineError, get_engine, MultiEngine  # noqa: E402
from engines.youtube_music import YoutubeMusicEngine  # noqa: E402
from naming import target_path  # noqa: E402
from postprocess import PostProcessor  # noqa: E402
from jobqueue import Queue  # noqa: E402
from resolvers import detect, match, spotify_tracklist, youtube_tracklist  # noqa: E402

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
with open(CFG_PATH, encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

API_TOKEN = CFG.get("api", {}).get("token") or ""

engine = get_engine(CFG)
post = PostProcessor(CFG)
# YT Music engine used directly by the URL resolver for art-track matching —
# a pasted Spotify track must land on the clean studio version, not on a
# JioSaavn cover (merged search would let covers in via duration ties).
ytm = YoutubeMusicEngine(CFG)


def _resolve_url(url: str) -> tuple[dict, dict]:
    """detect(url) + full tracklist; raises EngineError on bad input."""
    d = detect(url)
    if not d:
        raise EngineError(f"unsupported link: {url}")
    if d["source"] == "spotify":
        pl = spotify_tracklist(d["kind"], d["id"])
    else:
        pl = youtube_tracklist(url)
    return d, pl


def _qbit_engine():
    engs = engine.engines if isinstance(engine, MultiEngine) else [engine]
    for e in engs:
        if getattr(e, "name", None) == "qbit":
            return e
    return None


def _download_with_failover(pick: dict) -> tuple:
    """engine.download(pick); on failure automatically falls back to the qbit
    FLAC (torrent) tier. Returns (local_path, attempts_trail) — the trail lets
    the caller report exactly what happened (lossy fail -> FLAC fallback).

    The fallback reuses the failed pick's song title + real duration: qbit
    locates the packed album on the torrent index, then matches the exact song
    file inside it by title AND verifies duration against the reference (±5s).
    """
    attempts = []
    src = pick.get("source")
    try:
        local = engine.download(pick)
        attempts.append({"source": src, "status": "ok"})
        return local, attempts
    except Exception as exc:
        attempts.append({"source": src, "status": "failed", "error": str(exc)})
        qbit = _qbit_engine()
        if qbit is not None and src != "qbit":
            # no 'flac' suffix here — qbit.search() handles the flac-bias +
            # plain fallback so the query isn't over-constrained.
            q = f"{pick.get('artist', '')} {pick.get('title', '')}".strip()
            try:
                cands = qbit.search(q, limit=8)
                if not cands:
                    raise EngineError(f"no FLAC pack on torrent index for '{q}'")
                best = cands[0]  # search() returns healthiest (most seeders) first
                fp = dict(pick)
                fp.update(
                    source="qbit",
                    track_id=best["track_id"],      # apibay torrent id
                    info_hash=best["info_hash"],    # for the magnet
                    title=pick.get("title"),        # SONG title -> in-pack file match
                    album=best["title"],            # pack/album name
                )
                local = qbit.download(fp)
                attempts.append({"source": "qbit", "status": "ok",
                                 "note": "FLAC fallback", "pack": best["title"]})
                return local, attempts
            except Exception as fex:
                attempts.append({"source": "qbit", "status": "failed",
                                 "error": str(fex)})
        raise


def _download_one(pick: dict, query: str, album_hint: str = "",
                  tracknumber: int | None = None, force_album: str = "",
                  dir_artist: str | None = None) -> dict:
    """Download + post-process one pick. Shared by /api/download and the
    /api/playlist loop so both paths tag identically.

    force_album: overrides the track's own album tag (album-link downloads —
    every track belongs to the linked album, and YT Music's per-track page
    tag may point at a single/compilation instead).
    dir_artist: album-artist override for the directory only (album stays
    together under one artist folder while track artist tags stay true).
    """
    local, attempts = _download_with_failover(pick)  # Path to downloaded audio
    meta = engine.meta(local, pick, query)
    if force_album:
        meta["album"] = force_album
    elif album_hint and not meta.get("album"):
        meta["album"] = album_hint
    if tracknumber is not None:
        meta["tracknumber"] = tracknumber
    # YouTube art-track picks sometimes carry no thumbnail — the /vi/ endpoint
    # serves a deterministic JPEG for any video id.
    if not meta.get("cover_url") and pick.get("source") == "youtube_music" and pick.get("track_id"):
        meta["cover_url"] = f"https://i.ytimg.com/vi/{pick['track_id']}/maxresdefault.jpg"

    lyrics = post.lyrics(meta["artist"], meta["title"])
    cover = post.cover(meta.get("release_mbid"), meta["artist"], meta.get("album") or "")
    if not cover and meta.get("cover_url"):
        # engine-provided art (e.g. JioSaavn album art) when MusicBrainz misses;
        # retry with hqdefault when maxres doesn't exist for the video.
        for cu in dict.fromkeys([meta["cover_url"],
                                 meta["cover_url"].replace("maxresdefault", "hqdefault")]):
            try:
                with urllib.request.urlopen(cu, timeout=15) as r:
                    cover = r.read()
                if cover:
                    break
            except Exception:
                cover = None

    dest = target_path(meta, CFG["output"]["root"], local.suffix, dir_artist=dir_artist)
    album_dir = os.path.dirname(dest)
    os.makedirs(album_dir, exist_ok=True)
    # staging lives on / (root fs) while the library is on /mnt/hdd — different
    # devices, so os.replace would raise EXDEV. shutil.move copies across mounts.
    shutil.move(str(local), dest)
    post.apply(dest, meta, lyrics, cover)
    post.write_folder_jpg(album_dir, cover)

    return {
        "query": query,
        "picked": pick,
        "output": dest,
        "format": local.suffix,
        "meta": {k: v for k, v in meta.items() if v and k != "unknown_album"},
        "lyrics": bool(lyrics and (lyrics.get("synced") or lyrics.get("plain"))),
        "cover": cover is not None,
        "attempts": attempts,
    }


def run_job(payload: dict) -> dict:
    kind = payload.get("kind")
    if kind == "search":
        cands = engine.search(payload["query"])
        if not cands:
            raise EngineError(f"no results for: {payload['query']}")
        return {"candidates": cands, "count": len(cands)}

    if kind == "resolve":
        d, pl = _resolve_url(payload["url"])
        if d["kind"] == "track" and d["source"] == "youtube":
            pick = ytm.video_pick(d["id"])
            return {"kind": "track", "source": "youtube",
                    "name": pick.get("title") or d["id"], "pick": pick}
        for t in pl["tracks"]:
            t["pick"] = match(ytm, t, authoritative_duration=(d["source"] == "spotify"))
        matched = sum(1 for t in pl["tracks"] if t.get("pick"))
        return {"kind": d["kind"], "source": d["source"], "name": pl["name"],
                "tracks": pl["tracks"], "total": len(pl["tracks"]), "matched": matched}

    if kind == "playlist":
        d, pl = _resolve_url(payload["url"])
        if d["kind"] == "track" and d["source"] == "youtube":
            # a single-link paste -> just download it
            pick = ytm.video_pick(d["id"])
            return _download_one(pick, pick.get("title") or d["id"])
        # Album links (YT Music OLAK album auto-playlists, Spotify albums)
        # mean every track belongs to ONE album: force the album tag and keep
        # the whole album under its main artist's folder. Plain user playlists
        # keep each track's own album/artist.
        is_album = d["id"].startswith("OLAK5uy_") or (d["source"] == "spotify" and d["kind"] == "album")
        dir_artist = None
        if is_album and pl["tracks"]:
            # Album artist = majority artist across the tracklist (the first
            # track may be a collab — LEO-NiNE opens with the PABLO duet, but
            # the album is LiSA's). The whole album folder hangs under that.
            counts = Counter(t.get("artist") for t in pl["tracks"] if t.get("artist"))
            if counts:
                dir_artist = counts.most_common(1)[0][0]
        outputs, errors = [], []
        for i, t in enumerate(pl["tracks"], 1):
            pick = t.get("pick") or match(ytm, t, authoritative_duration=(d["source"] == "spotify"))
            if not pick:
                errors.append({"track": f"{t['artist']} - {t['title']}", "error": "no match"})
                continue
            try:
                # album links: force the album title; spotify tracks in plain
                # playlists carry their own album; youtube playlists fall back
                # to the playlist title.
                if is_album:
                    hint, force = pl["name"], pl["name"]
                elif d["source"] == "spotify":
                    hint, force = t.get("album") or "", ""
                else:
                    hint, force = pl["name"], ""
                out = _download_one(pick, pick.get("query") or f"{t['artist']} {t['title']}",
                                    album_hint=hint, force_album=force,
                                    tracknumber=t.get("tracknumber") or i,
                                    dir_artist=dir_artist)
                outputs.append(out)
            except Exception as exc:  # one bad track must not kill the dump
                errors.append({"track": f"{t['artist']} - {t['title']}", "error": str(exc)})
        return {"name": pl["name"], "total": len(pl["tracks"]),
                "downloaded": len(outputs), "errors": errors, "outputs": outputs}

    # download + post-process
    return _download_one(payload["pick"], payload.get("query", ""))


queue = Queue(run_job)
app = FastAPI(title="MusicFetch", version="0.1")


def _check_api_key(x_api_key: str | None):
    if API_TOKEN and x_api_key != API_TOKEN:
        raise HTTPException(401, "invalid API key")


class SearchReq(BaseModel):
    query: str


class DownloadReq(BaseModel):
    query: str
    pick: dict


class UrlReq(BaseModel):
    url: str


@app.get("/health")
def health():
    return {"ok": True, "time": time.time()}


@app.post("/api/search")
def api_search(req: SearchReq, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    if not req.query.strip():
        raise HTTPException(400, "query required")
    job_id = queue.submit({"kind": "search", "query": req.query.strip()})
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/download")
def api_download(req: DownloadReq, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    if not req.query.strip() or not req.pick:
        raise HTTPException(400, "query and pick required")
    job_id = queue.submit({"kind": "download", "query": req.query.strip(), "pick": req.pick})
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/resolve")
def api_resolve(req: UrlReq, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    if not req.url.strip():
        raise HTTPException(400, "url required")
    job_id = queue.submit({"kind": "resolve", "url": req.url.strip()})
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/playlist")
def api_playlist(req: UrlReq, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    if not req.url.strip():
        raise HTTPException(400, "url required")
    job_id = queue.submit({"kind": "playlist", "url": req.url.strip()})
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/job/{job_id}")
def api_job(job_id: str, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    job = queue.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


@app.get("/api/jobs")
def api_jobs(x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    return queue.all()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CFG["api"]["host"], port=CFG["api"]["port"])
