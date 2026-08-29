|# MusicFetch — FLAC research round 2 (2026-08-22, live-probed)

**Verdict up front: the torrent route works on this box. End-to-end proven today.**

## What died since last check
- lucida.to → Cloudflare 403 challenge (headless unpassable)
- squid.wtf → pivoted to a tools portal (debrid/KHInsider/JioSaavn), Tidal downloader gone
- doubledouble.top → alive but gated by Turnstile captcha + hCaptcha; `/resolve` errors; API not automation-friendly
- Public hifi-api instances (both Render ones in spotube-plugin's list) → "Service Suspended"
- spofree README re-confirms: Tidal mass-banning continues, incl. homelab hifi-api users. Route stays dead.

## What WORKS (proven live, 23:15 IST)

**Public trackers + apibay + aria2c selective download.**

Recipe:
1. `apibay.org/q.php?q=<album>+<artist>` (PirateBay mirror API, no auth) → torrents with seeders
2. `apibay.org/f.php?id=<id>` → per-file listing BEFORE downloading; pick the one track wanted
3. magnet with tracker args (`&tr=udp://tracker.opentrackr.org:1337/announce...`) — pure DHT is slow here (0 peers in 100s), trackers are mandatory
4. `aria2c --select-file=N --seed-time=0` downloads ONE file from the album torrent

Proof of concept run:
- RAM 10th Anniversary FLAC torrent, 108 seeders, metadata in **4s** via opentrackr
- Selected only track #8 → got full `Get Lucky.flac`, 43.5MB, ~450KB/s+
- Parsed STREAMINFO manually: **44100 Hz / 16-bit / stereo / 369.6s — genuine CD rip**
- Torrent included auCDtect.txt + .accurip + .log = verified lossless, not transcode

## Integration notes for engines/torrent.py
- apibay rate-limits hard (429 after a few calls) — cache results, backoff, don't hammer
- Search bias: append "flac" to query, prefer VIP/trusted uploaders, seeders>20
- Quality gate: verify STREAMINFO (16/44.1 or 24/96) post-download before accepting; reject if duration mismatches MusicBrainz ref by >5s
- Transcode tell: FLAC that decodes to blocky MP3-era spectrum — trust auCDtect/accurip presence as bonus signal
- aria2c is already installed on box; runs fine headless
- qBittorrent-nox also running (port 1340 WebUI, user qbittorrent, password unknown) — ignore it, use aria2c directly
- Cleanup: staging dir must remove sibling files/aria2 control files after extracting the one track
- Legal note for PLAN: same category as everything else we do; private trackers (RED etc.) would be better quality-wise but need invites — not pursuing

## Recommendation
Engine order becomes: jiosaavn (fast, reliable) → youtube_music → **torrent (FLAC when requested)**.
Torrent engine is the only free FLAC path left standing; everything else in the ecosystem is banned/captcha'd/dead.
