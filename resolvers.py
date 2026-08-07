"""URL resolvers — turn pasted links into track lists for MusicFetch.

YouTube / YouTube Music:
  single links (watch, youtu.be, shorts, music.youtube.com song) -> 1 track
  playlists (youtube.com/playlist, music.youtube.com/playlist including the
  OLAK5uy_ album auto-playlists) -> N tracks via yt-dlp flat extraction

Spotify (track / album / playlist):
  resolved through the public embed widget page open.spotify.com/embed/
  <kind>/<id>, which server-renders the full entity + tracklist into a
  __NEXT_DATA__ JSON blob (name, per-track uri/title/artists/duration).
  No API token, no auth, no relay needed — the token endpoints are blocked
  (clienttoken handshake 400 / get_access_token 403) but the embed page
  serves plain HTML to any client. Durations are authoritative (Spotify
  album masters) and drive the min-diff pick in match().

Every resolved track is matched back to the YT Music engine (art-track
search) so playlist downloads always land the clean, full-length studio
version instead of whatever video the playlist points to. For Spotify
references the album duration is authoritative and drives a min-diff pick;
for YouTube playlist entries the entry durations are unreliable (CLiPs,
edits, TV sizes) so the top search hit wins.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

import yt_dlp

logger = logging.getLogger(__name__)

# --- link classification ------------------------------------------------

_YT_TRACK = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/|"
    r"music\.youtube\.com/(?:watch\?(?:.*&)?v=|song/))([A-Za-z0-9_-]{11})"
)
_YT_LIST_PARAM = re.compile(r"[?&]list=([A-Za-z0-9_-]{13,})")
_SP_TRACK = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?track/|spotify:track:)([A-Za-z0-9]{22})"
)
_SP_ALBUM = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?album/|spotify:album:)([A-Za-z0-9]{22})"
)
_SP_PLAYLIST = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?playlist/|spotify:playlist:)([A-Za-z0-9]{22})"
)

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def detect(url: str) -> dict | None:
    """Classify a pasted link.

    Returns {"kind": "track"|"album"|"playlist", "source": "youtube"|"spotify",
    "id": ..., "url": ...} or None when the URL isn't recognised.
    """
    u = (url or "").strip()
    if not u:
        return None

    for rx, kind in ((_SP_TRACK, "track"), (_SP_ALBUM, "album"),
                     (_SP_PLAYLIST, "playlist")):
        m = rx.search(u)
        if m:
            return {"kind": kind, "source": "spotify", "id": m.group(1), "url": u}

    # youtube: a playlist context only when the URL explicitly says /playlist
    # or the list id is an OLAK5uy_ album auto-playlist. A watch?v=X&list=PL...
    # share link is the single video the user actually clicked.
    lm = _YT_LIST_PARAM.search(u)
    if lm and (lm.group(1).startswith("OLAK5uy_") or "/playlist" in u.split("?")[0]):
        return {"kind": "playlist", "source": "youtube", "id": lm.group(1), "url": u}
    m = _YT_TRACK.search(u)
    if m:
        return {"kind": "track", "source": "youtube", "id": m.group(1), "url": u}
    return None


# --- youtube / ytmusic --------------------------------------------------

_CLIP_MARKERS = re.compile(
    r"\s*[-–—:]*\s*(?:MUSiC CLiP|Music Video|Official Video|Lyric Video|"
    r"Official Audio|YouTube EDIT ver\.?|TV ver\.?|Promo Video).*$",
    re.IGNORECASE,
)
_JP_BRACKETS = re.compile(r"[【［\[\(（][^】］\]\)）]*[】］\]\)）]")
_QUOTES = re.compile(r"[\"'“”‘’「」『』]")


def _clean_channel(name: str) -> str:
    for suffix in (" - Topic", " Official YouTube", " Official", " VEVO"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def _clean_title(title: str) -> str:
    """Strip CLiP/EDIT/ver./bracket noise from playlist entry titles so the
    YT Music search lands the plain studio track."""
    t = _CLIP_MARKERS.sub("", title)
    t = _JP_BRACKETS.sub(" ", t)
    t = _QUOTES.sub("", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def youtube_tracklist(playlist_url: str) -> dict:
    """yt-dlp flat extraction of a playlist/album URL -> {name, tracks}."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
    entries = [
        e for e in (info.get("entries") or [])
        if isinstance(e, dict) and _VIDEO_ID.match(e.get("id") or "")
    ]
    tracks = []
    for e in entries:
        artist = _clean_channel(e.get("channel") or e.get("uploader") or "")
        title = _clean_title(e.get("title") or "")
        # CLiP titles often lead with the artist name ("LiSA "Makotoshiyaka"")
        # — strip it so the search query is "LiSA Makotoshiyaka", not the
        # doubled "LiSA LiSA ..." that drifts the top-6 ranking.
        if artist and title.lower().startswith(artist.lower() + " "):
            title = title[len(artist) + 1:].strip()
        tracks.append({
            "artist": artist,
            "title": title,
            "duration": int(e.get("duration") or 0),
            "ref": e.get("id"),
        })
    return {"name": info.get("title") or "Playlist", "tracks": tracks}


# --- spotify ------------------------------------------------------------


class SpotifyError(Exception):
    """Embed-page fetch/parse failures for the Spotify resolver."""


_EMBED_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_SPOTIFY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _embed_entity(kind: str, sid: str) -> dict:
    url = f"https://open.spotify.com/embed/{kind}/{sid}"
    req = urllib.request.Request(url, headers={"User-Agent": _SPOTIFY_UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        html = r.read().decode("utf-8", "replace")
    m = _EMBED_DATA.search(html)
    if not m:
        raise SpotifyError("embed page missing __NEXT_DATA__")
    data = json.loads(m.group(1))
    return data["props"]["pageProps"]["state"]["data"]["entity"]


def spotify_tracklist(kind: str, sid: str) -> dict:
    """Resolve a Spotify track/album/playlist via the embed widget page.

    The embed page server-renders entity.name and either entity.trackList
    (playlist/album: per-track uri, title, subtitle=artists, duration in ms)
    or the entity itself (single track: title + artists[]). No token, no
    auth. Durations are authoritative album masters, so callers should pass
    authoritative_duration=True to match(). Playlist entries carry no album
    name here (the Web API did) — the YT Music page tag fills it instead.
    """
    ent = _embed_entity(kind, sid)
    name = ent.get("name") or ent.get("title") or "Spotify"
    raw = ent.get("trackList") or ([ent] if kind == "track" else [])
    if not raw:
        raise SpotifyError(f"no tracklist for {kind}:{sid}")
    tracks = []
    for i, t in enumerate(raw):
        artists = t.get("subtitle") or "; ".join(
            a.get("name", "") for a in (t.get("artists") or []))
        tracks.append({
            "artist": re.sub(r"\s+", " ", artists).replace("\u00a0", " ").strip(),
            "title": t.get("title") or t.get("name") or "",
            "album": name if kind == "album" else "",
            "duration": int((t.get("duration") or 0) // 1000),
            "tracknumber": (i + 1) if kind == "album" else 0,
            "ref": (t.get("uri") or "").rsplit(":", 1)[-1] or t.get("id") or "",
        })
    return {"name": name, "tracks": tracks}


# --- matching -----------------------------------------------------------


_VERSION_MARKERS = re.compile(r"\b(?:instrumental|karaoke|live|acoustic|remix|cover|edit|tv|radio)\b|ver\.")


def _artist_ok(cand_artist: str, query_artist: str) -> bool:
    """Candidate artist must share a token with the query artist (kills
    cover-artist drift, e.g. 'Sati Akura' for a LiSA track). Lenient on
    purpose: JP/Latin script mixes and multi-artist strings ('LiSA; PABLO')."""
    qa = (query_artist or "").strip()
    if not qa:
        return True
    ca = (cand_artist or "").lower()
    return any(tok and tok in ca for tok in re.split(r"[,;/\s]+", qa.lower()))


_TITLE_TOKENS = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _title_overlap(cand_title: str, ref_title: str) -> bool:
    """True when the candidate and the reference share a title token (kills
    duration-close wrong songs like 'Mind Blowing' for 'Howl'). JP titles
    legitimately fail this (romaji vs kanji) — callers must fall back."""
    rt = {t.lower() for t in _TITLE_TOKENS.findall(ref_title) if len(t) > 1}
    ct = {t.lower() for t in _TITLE_TOKENS.findall(cand_title) if len(t) > 1}
    return bool(rt & ct)


def match(ytm_engine, track: dict, authoritative_duration: bool) -> dict | None:
    """Map a resolved track to the best YT Music art-track candidate.

    Selection order:
      1. artist-agreement filter (kills covers)
      2. version-marker filter (kills instrumental/live/karaoke unless the
         reference asks for one)
      3a. authoritative (Spotify) refs — the album duration is exact: pick
          the candidate closest to it, but only within 45s; beyond that the
          true track is missing, fall back to the top survivor.
      3b. non-authoritative (YouTube) refs — trust the SONGS-filter ranking
          (art tracks first). The ref duration only overrides the ranking
          when the top hit is implausibly far from it (>60s) AND another
          survivor is within 60s (CLiP refs carry extended/edited lengths,
          so raw min-diff picks wrong same-length songs — the Akeboshi case).
    """
    q = f"{track.get('artist', '')} {track.get('title', '')}".strip()
    if not q:
        return None
    cands = ytm_engine.search(q, limit=8)
    if not cands:
        return None
    # 1. artist agreement
    cands = [c for c in cands if _artist_ok(c.get("artist") or "", track.get("artist") or "")]
    # 2. version markers
    ref_title = (track.get("title") or "").lower()
    want_marked = bool(_VERSION_MARKERS.search(ref_title))
    if not want_marked:
        clean = [c for c in cands if not _VERSION_MARKERS.search((c.get("title") or "").lower())]
        if clean:
            cands = clean
    if not cands:
        return None

    ref = int(track.get("duration") or 0)
    if authoritative_duration:
        if ref:
            # Prefer the closest candidate that shares a title token with the
            # reference — same-duration wrong songs tie min-diff at 0s (One
            # Last Time for "hate that i made you love me", the Spanish
            # "Lo Arriesgo Todo" for "Risk It All"). Only when nothing
            # overlaps (JP romaji/kanji mismatch) fall back to plain min-diff.
            overlap = [c for c in cands if _title_overlap(c.get("title") or "", track.get("title") or "")]
            pool = overlap if overlap else cands
            best = min(pool, key=lambda c: abs((c.get("duration") or 0) - ref))
            if abs((best.get("duration") or 0) - ref) > 45:
                best = cands[0]  # true track absent — top survivor is least-bad
        else:
            best = cands[0]
    else:
        # Non-authoritative (YouTube) refs. Trust the SONGS-filter ranking,
        # with two corrections:
        #  - ref >= 200s (full-length CLiP): ranking first; only let duration
        #    override when the top hit is implausibly far (>60s) from the ref
        #    and another survivor is within 60s (Makotoshiyaka's CLiP ref is
        #    259s but the studio track is 238s — a naive min-diff picked
        #    Akeboshi at 269s).
        #  - ref < 200s (TV-size/EDIT refs): the ref length is not a target —
        #    prefer a title-overlapping candidate (kills 'Mind Blowing' for
        #    'Howl'); only if nothing overlaps (JP romaji/kanji mismatch)
        #    fall back to duration-close, then to ranking.
        if ref >= 200:
            best = cands[0]
            if abs((best.get("duration") or 0) - ref) > 60:
                near = [c for c in cands if abs((c.get("duration") or 0) - ref) <= 60]
                if near:
                    best = min(near, key=lambda c: abs((c.get("duration") or 0) - ref))
        else:
            overlap = [c for c in cands if _title_overlap(c.get("title") or "", track.get("title") or "")]
            if overlap:
                best = overlap[0]
            else:
                near = [c for c in cands if abs((c.get("duration") or 0) - ref) <= 25]
                best = near[0] if near else cands[0]
    best["query"] = q
    return best
