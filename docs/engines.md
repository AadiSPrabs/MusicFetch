# Engines & source ecosystem

MusicFetch's sources are **pluggable engines** behind one interface
(`engines/__init__.py`). `config.yaml → engine.order` decides the merge
order for search and the routing for downloads (each candidate carries a
`source` field).

`engine.order` is also your **quality-priority lever** — edit it and restart
the API to change which source you prefer. Default is `[qbit, jiosaavn,
youtube_music]` (FLAC first). When a lossy source **fails** to download,
MusicFetch automatically fails over to the higher-quality tier — see
[Download failover](#download-failover).

Current engines (all live-verified):

| Engine | Quality | Auth | Notes |
|---|---|---|---|
| `jiosaavn` | **320 kbps AAC** (~323 kbps measured) | none | Public API; best for Bollywood/Hindi + general |
| `youtube_music` | ~128 kbps AAC (fmt 140) | none | yt-dlp; covers everything else (JP, niche, regional) |
| `qbit` (FLAC tier) | **lossless 16/44.1 FLAC** (CD rips) | local qBittorrent WebUI | Torrent-based; live-verified end-to-end — see below |

> **FLAC status (research round 2, 2026-08-22): the torrent route is proven.**
> Every stream-ripping route is dead again this month (see history), but
> public-tracktor torrents work from this box end-to-end: apibay search →
> per-file listing → selective download of one track → verified genuine CD
> rip (44.1 kHz / 16-bit, auCDtect + AccurateRip logs present in the pack).
> The engine will be `engines/qbit.py`, driving the qBittorrent WebUI v5.1.0
> already running on this box. Full findings:
> [`flac-research.md`](flac-research.md). The tagging/filing pipeline is
> format-agnostic (FLAC/MP3/M4A all supported in `postprocess.py`), so no
> other layer changes.

## qBittorrent engine (`engines/qbit.py`, FLAC tier) — LIVE

Drives the **existing local qBittorrent-nox** (v5.1.0, WebUI at
`127.0.0.1:1340`, runs as user `qbittorrent`) over its REST API — no new
daemon, no aria2c subprocess. Verified end-to-end through the real API
(see `flac-research.md` round 3): Get Lucky, selective single-file
download from a 712MB pack in ~70s total, tagged + filed correctly.

1. **Search**: `apibay.org/q.php?q=<query>` (PirateBay mirror API, keyless).
   apibay's search is **AND-token** — appending `flac` over-constrains and
   silently empties multi-word queries, so the engine runs `{query} flac`
   first (keeps FLAC bias) and **falls back to plain `{query}`** when that
   returns nothing. Seed floor is relaxed to 1 (healthiest survivor wins) —
   a low-seed pack is an offer, not a rejection. ⚠ apibay is flaky (HTTP 429
   bursts + read timeouts); both search and file-listing retry with backoff
   and cache results so a blip never silently kills the tier.
2. **Per-file listing**: `apibay.org/f.php?id=<id>` → file names + sizes
   *before* downloading anything. Pick the wanted track by name + duration
   match against the reference (±5s). ⚠ `f.php` returns fields **dict-encoded**
   (`{"0": value}`, not lists), and many fringe packs return
   **"Filelist not found"** — the selective-download model can't proceed
   without the file list, so those packs are *unfetchable* even when search
   finds them. The engine reports that cleanly rather than failing silently.
3. **Download**: build magnet with explicit tracker args —
   `&tr=udp://tracker.opentrackr.org:1337/announce` etc. **Pure DHT found
   zero peers in 100s on this network; trackers are mandatory**, not
   optional. `POST /api/v2/torrents/add`.
4. **Selective download**: once metadata lands, fetch
   `/api/v2/torrents/files` (GET only) then flip unwanted files to
   priority 0 via `/api/v2/torrents/filePrio` (wanted file = 6; priority
   4 is rejected by qbt v5). Metadata readiness is detected by the files
   endpoint stopping its 400 — `metadata_received` is unreliable in v5.
5. **Quality gate**: STREAMINFO must parse as FLAC with duration within
   ±5s of the reference before the file is accepted into staging.
6. **Completion**: poll `/api/v2/torrents/info` → move the finished file
   to staging → normal postprocess (tags/lyrics/cover/rename) → delete
   the torrent from the client (seed-time 0 policy).

Client facts verified live: savepath `/mnt/dietpi_userdata/downloads`
(must stay owned by user `qbittorrent`), login OK. Credentials live only
in the gitignored `config.yaml`.

## Download failover

Downloads funnel through `_download_with_failover()` in `api.py`:

1. Try the candidate's own source first.
2. If it throws, fall back to the **next higher-quality tier** (the qbit FLAC
   engine when present) — reusing the failed pick's clean title + real
   duration to locate the exact song file inside a packed album and verify it
   (the ±5s duration gate rejects wrong CD/live duplicates).
3. The job result carries an **`attempts` trail** —
   `[{source, status, error|note}, ...]` — so the Telegram bot can report what
   actually happened: "ytmusic ✘ 403 → qbit ✔ FLAC fallback".

## How each engine works

### jiosaavn (`engines/jiosaavn.py`)

Uses JioSaavn's public API (`api.php`) with zero auth:

1. `search.getResults` → candidates (id, title, artists, album, duration,
   `320kbps` flag, artwork).
2. `song.getDetails` by id → `encrypted_media_url`.
3. DES-ECB decrypt (key `38346591`) → CDN URL like `..._{96,160,320}.mp4`.
4. The anonymous API only *serves* 96 kbps, but the CDN URL is full-length
   at any quality — swap the suffix and stream the 320 kbps file.
   Falls back down the chain (160 → 96) on 404.
5. Files are **AAC-in-MP4** → saved as `.m4a`.

Gotchas baked into the code: search param is `q` (not `query`); `artistMap` /
`duration` / `album` / `320kbps` live under `more_info`; `song.getDetails`
returns a dict **keyed by song id**, not a list.

### youtube_music (`engines/youtube_music.py`)

yt-dlp against `music.youtube.com`'s song catalog:

1. Search via `music.youtube.com/search?q=...` with the **Songs filter**
   (`sp=EgWKAQIIAWoKEAoQAxAEEAkQBQ%3D%3D`) so results bias to clean art
   tracks instead of videos/lives/covers.
2. Flat extract → filter to 11-char video ids (rejects `UC…` channels,
   `VL…`/`PL…` playlists) → full metadata pass per video for
   track/artist/album straight off the YT Music page.
3. Download **fmt 140** (m4a / AAC-LC ~128 kbps) — deliberately *not*
   `bestaudio` (opus) so the M4A tagging pipeline applies consistently.
4. Player-client retries: on 403/503, retries `default → android →
   web_embedded` (some videos are SABR-only on one client and
   images-only on another). If all three fail, the video is locked at that
   moment — retry later or pick an alternate video id.

## Link ingestion (`resolvers.py`)

Pasted links are classified, resolved to track lists, then **re-matched to
the clean YT Music art track** — playlist videos are never downloaded raw
(because YouTube album auto-playlists mix in MUSiC CLiPs, TV-size edits,
and covers).

- **YouTube**: `watch`/`shorts`/`embed`/`live`/`youtu.be`/
  `music.youtube.com/song` → single track. `/playlist` or `OLAK5uy_` list id
  → playlist. A `watch?v=X&list=PL…` share link is the video the user
  clicked — treated as a track.
- **Spotify**: resolved via the **public embed widget**
  (`open.spotify.com/embed/{track|album|playlist}/{id}`), which
  server-renders the entity + tracklist into a `__NEXT_DATA__` JSON blob.
  No token, no relay — the Web API token path 403s/400s from residential
  IPs, but the embed page serves plain HTML to anyone. Spotify durations
  are authoritative album masters and drive the duration-based match.
- **Matching** (`match()` in `resolvers.py`): artist-agreement filter
  (kills cover drift), version-marker filter (drops instrumental/live/
  karaoke/covers unless the reference asks for one), then duration-aware
  selection — Spotify refs use min-diff ≤45s with a title-token tiebreak;
  YouTube refs trust the SONGS ranking and use duration only as a sanity
  check. Titles/channels are cleaned (strip `MUSiC CLiP | YouTube EDIT ver. |
  Official Video` + JP brackets + quotes) before querying.
- **Albums**: every track of a Spotify album or YT Music `OLAK5uy_` album
  is forced into the album folder under the *majority* artist of the
  tracklist (a collab opener must not hijack the folder); per-track artist
  tags stay true.

## Post-processing (`postprocess.py`)

All free + keyless:

- **MusicBrainz** (`/ws/2/recording`) — canonical title/artist/album/year,
  track number, release MBID. Requires a proper User-Agent
  (`musicbrainz_ua` in config) — bare clients get 403. 1 req/s politeness.
- **Cover Art Archive** (`coverartarchive.org/release/{mbid}/front-500`) —
  release artwork; falls back to engine-provided art (JioSaavn 500×500,
  `i.ytimg.com/vi/…/maxresdefault.jpg`).
- **LRCLIB** (`lrclib.net/api/search`) — synced + plain lyrics, embedded in
  the file *and* written as a `.lrc` sidecar (Jellyfin's most reliable
  lyrics path).
- **mutagen** — FLAC / ID3 (MP3) / MP4 atoms, truthy-only overwrite.
  WebP covers are converted to JPEG via ffmpeg before embedding (players
  can't decode WebP labeled as JPEG).
- `folder.jpg` written per album dir for Jellyfin.

## History: what was tried and removed

The engine list today is the survivor of a month of live probing
(Jul–Aug 2026). All of these were built, tested, and **scrapped** — the
code is gone, not dormant:

- **soulseek/slskd** — the only free lossless source, fully deployed, then
  proven unusable from this network: the box sits behind **Jio CGNAT** and
  Soulseek needs inbound connectivity (port 2234) end-to-end. Container +
  engine removed.
- **qobuz** — login works, but free accounts get **30-second previews only**
  (full tracks require a paid tier). Scrapped.
- **deezer** — the ARL route needs a HiFi account and Deezer signup is
  geo-blocked for India (even via VPN). Scrapped.
- **Tidal public hifi-api instances** (qqdl.site / lucida.to / spofree) —
  dead: Cloudflare-challenged headless, and maintainers confirm mass
  account bans. **Re-checked 2026-08-22: still dead.** lucida.to now
  serves a Cloudflare 403 challenge; both public Render hifi-api
  instances in the spotube-plugin list are "Service Suspended";
  spofree's README re-confirms homelab users are banned too.
- **Public downloader sites** — squid.wtf pivoted to a tools portal (no
  Tidal downloader anymore); doubledouble.top is alive but gated behind
  Turnstile + hCaptcha and its API errors out — captcha-solving is not a
  maintainable automation dependency.
- **Verome-API** — public instance sunset (Deno Deploy Classic EOL),
  self-hosting adds proxies for the same ~128 kbps ceiling yt-dlp already
  gives directly.

Lesson encoded in the design: **sources churn monthly**. The owned layer —
naming, tagging, lyrics, API, bot — works regardless of which source is
alive, and adding a new source is one new `engines/<name>.py` + a config
line.

> **Educational note:** this document describes unofficial, reverse-engineered
> aspects of third-party services (JioSaavn's API quality tiers, Spotify's
> embed page) for educational purposes. They exist here to show how
> source-pluggable download architectures are built — not to encourage
> circumventing anyone's terms of service. Use responsibly, personally, and
> non-commercially.
