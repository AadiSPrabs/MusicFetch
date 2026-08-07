# MusicFetch — self-hosted music downloader

Search, download, tag, and organize **single songs — or entire albums and
playlists** — into a Jellyfin-ready library, controlled from **Telegram** and
a **REST API**. Self-hosted, no accounts, no ads — just pick a track or
paste a link and it lands on disk as `Artist/Album/Song.m4a` with proper
metadata, embedded cover art, and lyrics.

```
search / paste a link
        │
        ▼
┌─────────────────┐   ┌──────────────────────┐   ┌─────────────────────┐
│   MultiEngine   │   │  PostProcessor       │   │  Library            │
│  jiosaavn (320) │──►│  MusicBrainz metadata│──►│ /Artist/Album/      │
│  youtube_music  │   │  LRCLIB lyrics       │   │   Song.m4a + .lrc   │
│  (~128 AAC)     │   │  cover embed         │   │   folder.jpg        │
└─────────────────┘   └──────────────────────┘   └─────────────────────┘
        │                                                │
        └── controlled by: Telegram bot ◄──┘
                           REST API (127.0.0.1:8090)
```

## What it is

MusicFetch is a Lidarr-style service: **any track — a single song or a whole
album/playlist — downloaded in full, tagged correctly, filed correctly.**

- **Dual engine search** — results from JioSaavn (320 kbps AAC, no auth) and
  YouTube Music (yt-dlp, ~128 kbps AAC) are merged into one list; each
  candidate is tagged with its source and downloads route automatically.
- **Paste-a-link ingestion** — drop in a YouTube, YouTube Music, or Spotify
  link (single track, album, or playlist) and MusicFetch resolves it, matches
  every track to the clean studio version, and downloads everything.
- **Real metadata pipeline** — MusicBrainz (artist/album/track/year), LRCLIB
  (synced + plain lyrics, embedded + `.lrc` sidecar), cover art (embedded +
  `folder.jpg` for Jellyfin).
- **Jellyfin-native layout** — files land as `Artist/Album/Song.m4a` with the
  multi-artist `Artist A; Artist B` convention preserved.
- **Two control surfaces** — a Telegram bot (search → pick → download → the
  file is also sent back to you) and a REST API for anything else.
- **Zero accounts** — JioSaavn's public API and YouTube Music need no login.
  (Lossless FLAC is a future track; see [docs/engines.md](docs/engines.md).)

## What it's made of

```
musicfetch/
├── api.py              # FastAPI REST service + job orchestration
├── jobqueue.py         # in-process serial job queue (search/download/resolve)
├── naming.py           # {Artist}/{Album}/{Song}.m4a path + collision handling
├── postprocess.py      # MusicBrainz, LRCLIB, cover art, mutagen tagging
├── resolvers.py        # YouTube/Spotify link classification + tracklist extraction
├── engines/
│   ├── __init__.py     # engine registry + MultiEngine (merge/routing)
│   ├── jiosaavn.py     # JioSaavn engine (320 kbps AAC)
│   └── youtube_music.py# YouTube Music engine (yt-dlp)
├── tools/
│   └── tgbot.py        # Telegram bot (stdlib-only long-poll)
└── config.yaml         # everything: engine order, output dir, API key, bot token
```

FastAPI + uvicorn for the API, yt-dlp + plain `requests` for sources, mutagen
for tagging. The Telegram bot deliberately uses **no bot framework** — it's a
thread-per-message urllib long-poll, so the whole service runs in one venv
with no extra moving parts. See [docs/architecture](docs/) for details.

## Installation

```bash
git clone https://github.com/AadiSPrabs/MusicFetch.git
cd MusicFetch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Requirements: Python 3.11+, `ffmpeg` on PATH (used to convert WebP covers to
JPEG for embedding).

Configure `config.yaml` (see [Configuration](#configuration)), then run:

```bash
# REST API (127.0.0.1:8090)
.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8090

# Telegram bot (in a second terminal)
.venv/bin/python tools/tgbot.py
```

Production (systemd, both services, reboot-safe): see
[docs/deployment.md](docs/deployment.md).

## Configuration

All configuration lives in one `config.yaml` (template: `config.example.yaml`):

> **First run:** set `output.root` to where you want music saved (e.g. `~/Music`)
> — the example file carries a placeholder. `staging_dir` is a scratch area
> that gets auto-created; you can leave it alone.

| Key | Meaning |
|---|---|
| `engine.order` | Download-source priority, e.g. `[jiosaavn, youtube_music]` |
| `output.root` | **Change this first** — where downloads land: `{root}/{Artist}/{Album}/{Song}.m4a` |
| `postprocess.*` | Toggles for MusicBrainz / lyrics / cover steps |
| `api.host` / `api.port` | REST bind address (localhost by default) |
| `api.token` | Optional API key — if set, clients must send `X-API-Key` |
| `telegram_bot.token` | BotFather token |
| `telegram_bot.allowed_chats` | Chat-ID allowlist; everyone else is refused |

## Usage

### Telegram bot (easiest)

The bot is the friendliest front-end — search, pick, done. If you don't
already have a bot, make one in two minutes:

1. Message **@BotFather** on Telegram → `/newbot` → pick a name and handle
   → copy the token it gives you.
2. Put the token in `config.yaml` → `telegram_bot.token`.
3. Add your chat ID to `telegram_bot.allowed_chats` — message
   [@userinfobot](https://t.me/userinfobot) to see your ID, or run the bot
   and read it from the log. Only allowlisted chats get served.
4. Run `.venv/bin/python tools/tgbot.py`.

Then in chat:

- **Paste a link** — YouTube / YouTube Music / Spotify track, album, or
  playlist. MusicFetch resolves it and downloads everything.
- `search <query>` — find tracks across both engines
- `download N` — grab result N from the last search
- `/status` — queue state
- `/help` — this

Single tracks are also sent back to you as an audio message after they land.

### REST API (no bot needed)

The bot is just a front-end — everything it does is one HTTP call away, so
you can drive MusicFetch from `curl`, scripts, cron jobs, or your own app
(Jellyfin clients, web UIs, whatever):

```bash
# search
curl -X POST localhost:8090/api/search -d '{"query":"LiSA ADAMAS"}'
# → {"job_id":"a1b2c3d4e5f6","status":"queued"}

# poll the job — candidates land in result
curl localhost:8090/api/job/a1b2c3d4e5f6

# download a candidate (copy the pick object from the search result)
curl -X POST localhost:8090/api/download \
     -d '{"query":"LiSA ADAMAS","pick":{"source":"jiosaavn","track_id":"...","artist":"LiSA","title":"ADAMAS"}}'

# paste-a-link flows
curl -X POST localhost:8090/api/resolve   -d '{"url":"https://open.spotify.com/album/..."}'
curl -X POST localhost:8090/api/playlist  -d '{"url":"https://music.youtube.com/playlist?list=..."}'
```

Full endpoint reference: [docs/api.md](docs/api.md).

## Docs

- [docs/api.md](docs/api.md) — REST API reference (endpoints, job model, examples)
- [docs/deployment.md](docs/deployment.md) — systemd units, directory layout, ops notes
- [docs/engines.md](docs/engines.md) — how the download engines work + source ecosystem history

## Status / roadmap

- ✅ Search + download + tagging pipeline (JioSaavn 320 kbps AAC + YT Music)
- ✅ Link ingestion: Spotify (track/album/playlist), YouTube (track/playlist)
- ✅ Telegram bot + REST API + serial job queue
- ⏳ Lossless FLAC source (research track — torrent-based, not soulseek)
- ⏳ Optional: album-artist multi-disc handling, watch-folder triggers

## Disclaimer

**This project is published for educational purposes only.** It exists to
demonstrate API design, multi-engine search/fallback architecture, async job
queues, link resolution, and audio metadata pipelines. It is not affiliated
with, endorsed by, or connected to JioSaavn, Spotify, YouTube, or Google.

Use it only for personal, non-commercial listening. Respect the source
services' terms of service and the copyright of the music you download —
do not redistribute downloaded files. The quality suffixes, endpoints, and
APIs these engines rely on are unofficial and can change or break at any
time; engines are source-pluggable for exactly this reason.
