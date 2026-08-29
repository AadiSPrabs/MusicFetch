# MusicFetch REST API

Base URL: `http://127.0.0.1:8090` (bind + port from `config.yaml → api`).

## Auth

Optional. If `api.token` is set in `config.yaml`, every request must carry:

```
X-API-Key: <token>
```

Missing/mismatched keys get `401`. When `api.token` is empty, no auth is enforced.

## Job model

All work is async: you submit a job and poll for it. A job looks like:

```json
{
  "id": "a1b2c3d4e5f6",
  "status": "queued",            // queued | running | done | error
  "created": 1723000000.0,
  "updated": 1723000010.0,
  "payload": {"kind": "search", "query": "LiSA ADAMAS"},
  "result": null,                // populated when done
  "error": null                  // populated when error
}
```

Jobs run **serially** (one at a time) — polite to the sources, and the queue
never loses a job.

---

## `POST /api/search`

Search both engines (merged, deduped).

**Request:** `{"query": "LiSA ADAMAS"}`

**Result:** `{"candidates": [...], "count": N}`

Candidate shape:

```json
{
  "source": "jiosaavn",          // jiosaavn | youtube_music | qbit — routes the download
  "track_id": "6uEI9gj0",        // jiosaavn song id / yt video id / apibay torrent id
  "artist": "LiSA",
  "title": "ADAMAS",
  "album": "ASCA THE GREATEST",
  "duration": 231,
  "quality": "FLAC (lossless)",  // "FLAC (lossless)" | "AAC 320" | "AAC 160" | "AAC ~128"
  "plays": "1234567",
  "year": 2018,
  "cover_url": "https://...",
  "seeders": 122,                // qbit only — torrent health (higher = more reliable)
  "size_mb": 325.1               // qbit only — album pack size
}
```

qbit (FLAC) candidates represent an **album pack on the torrent index**, not
a single song: `artist` is `"?"`, `title` is the pack name, `seeders`/`size_mb`
tell you its health, and the exact song file is resolved *inside* the pack at
download time (title + duration match, ±5s gate).
```

## `POST /api/download`

Download + post-process one candidate.

**Request:**

```json
{
  "query": "LiSA ADAMAS",
  "pick": { "source": "jiosaavn", "track_id": "6uEI9gj0", "artist": "LiSA", "title": "ADAMAS", ... }
}
```

The `pick` is normally copied verbatim from a search candidate's result.

**Result:**

```json
{
  "query": "LiSA ADAMAS",
  "picked": {"source": "jiosaavn", "...": "..."},
  "output": "/mnt/hdd/media/music2test/LiSA/ASCA THE GREATEST/ADAMAS.m4a",
  "format": ".m4a",
  "meta": {"title": "ADAMAS", "artist": "LiSA", "album": "ASCA THE GREATEST", "tracknumber": "2", "date": "2018", "...": "..."},
  "lyrics": true,
  "cover": true,
  "attempts": [
    {"source": "youtube_music", "status": "failed", "error": "HTTP Error 403: Forbidden"},
    {"source": "qbit", "status": "ok", "note": "FLAC fallback", "pack": "LiSA — ADAMAS (2018) [FLAC]"}
  ]
}
```

`attempts` is the delivery trail (1 entry on a direct hit; 2+ when a lossy
source failed and a higher-quality tier grabbed it). `format` reflects what
was actually delivered (`.m4a`, `.flac`, …).
```

## `POST /api/resolve`

Inspect a pasted link **without downloading** — returns the track list and
the matched YT Music pick per track.

**Request:** `{"url": "https://open.spotify.com/album/..."}`

**Result (track):**

```json
{"kind": "track", "source": "youtube", "name": "ADAMAS", "pick": {"source": "youtube_music", "track_id": "...", "...": "..."}}
```

**Result (album / playlist):**

```json
{
  "kind": "album",
  "source": "spotify",
  "name": "ASCA THE GREATEST",
  "total": 13,
  "matched": 13,
  "tracks": [
    {"artist": "LiSA", "title": "ADAMAS", "duration": 231, "ref": "...", "pick": {"source": "youtube_music", "...": "..."}},
    "..."
  ]
}
```

Supported links: Spotify `track` / `album` / `playlist` (incl. `spotify:`
URIs and `intl-in/` paths), YouTube `watch` / `shorts` / `embed` / `live` /
`youtu.be` / `music.youtube.com` (track, playlist, and the `OLAK5uy_` album
auto-playlists). A `watch?v=X&list=PL...` share link resolves as **the single
video** — not its playlist.

## `POST /api/playlist`

Like `/api/resolve`, but downloads everything. One job loops the track list;
a bad track is recorded in `errors` and never aborts the dump.

**Request:** `{"url": "https://music.youtube.com/playlist?list=OLAK5uy_..."}`

**Result:**

```json
{
  "name": "ASCA THE GREATEST",
  "total": 13,
  "downloaded": 13,
  "errors": [],
  "outputs": [
    {"query": "LiSA ADAMAS", "picked": {"...": "..."}, "output": "/mnt/hdd/.../ADAMAS.m4a", "format": ".m4a", "meta": {"...": "..."}, "lyrics": true, "cover": true},
    "..."
  ]
}
```

## `GET /api/job/{job_id}`

Poll a job. `404` for unknown ids. Response is the full [job model](#job-model).

## `GET /api/jobs`

All jobs in the queue (past + present), newest first.

## `GET /health`

```json
{"ok": true, "time": 1723000000.0}
```

---

## Example flow (curl)

```bash
BASE=localhost:8090
[ -n "$API_TOKEN" ] && AUTH=(-H "X-API-Key: $API_TOKEN") || AUTH=()

# 1. search
JOB=$(curl -s "${AUTH[@]}" -X POST $BASE/api/search -d '{"query":"Queen Bohemian Rhapsody"}')
JOB_ID=$(echo "$JOB" | jq -r .job_id)
sleep 5

# 2. get candidates
CAND=$(curl -s "${AUTH[@]}" $BASE/api/job/$JOB_ID | jq -c '.result.candidates[0]')

# 3. download
curl -s "${AUTH[@]}" -X POST $BASE/api/download \
     -d "{\"query\":\"Queen Bohemian Rhapsody\",\"pick\":$CAND}"
```
