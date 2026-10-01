#!/usr/bin/env python3
"""MusicFetch Telegram bot — thin bridge between Telegram DMs and the
MusicFetch API on 127.0.0.1:8090.

Stdlib-only transport (urllib long-poll on getUpdates). One thread per
incoming message so a long-running download poll never blocks replies.
Owner allowlist from config.yaml -> telegram_bot.allowed_chats.

Run: .venv/bin/python tools/tgbot.py   (same venv as the API)
"""
from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tgbot")

BASE = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((BASE / "config.yaml").read_text())
API = f"http://{CFG['api']['host']}:{CFG['api']['port']}"
TOKEN = (CFG.get("telegram_bot") or {}).get("token") or ""
ALLOWED = set((CFG.get("telegram_bot") or {}).get("allowed_chats") or [])
if not TOKEN:
    raise SystemExit("telegram_bot.token missing in config.yaml")

TG = f"https://api.telegram.org/bot{TOKEN}"
_LINK_RE = re.compile(
    r"(https?://(?:[a-z0-9.-]*\.)?(?:youtube\.com|youtu\.be|music\.youtube\.com|"
    r"open\.spotify\.com|spotify\.link)/\S+|spotify:(?:track|album|playlist):\S+)"
)
_ERR_RE = re.compile(r"^(?:error|failed):?\s*(.*)$", re.I)

_picks: dict[int, dict[int, dict]] = {}  # chat_id -> {n: pick}
_alts: dict[int, dict[int, dict[str, dict]]] = {}  # chat_id -> {n: {source: candidate}}


def tg(method: str, **params) -> dict:
    """Call the Telegram Bot API. Files go as multipart when 'files' given."""
    files = params.pop("files", None)
    url = f"{TG}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in params.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
        for k, (fn, data) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                     f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
            body += data
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def api(method: str, body: dict | None = None, timeout: int = 60) -> dict:
    url = f"{API}{method}"
    if body is not None:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def say(chat: int, text: str, **kw):
    try:
        tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", **kw)
    except Exception as exc:
        log.warning("sendMessage failed: %s", exc)


def send_audio(chat: int, path: str, caption: str):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        say(chat, f"⚠️ file missing: {path} ({exc})")
        return
    try:
        tg("sendAudio", chat_id=chat, caption=caption[:1024],
           files={"audio": (Path(path).name, data)})
    except Exception as exc:
        say(chat, f"⚠️ upload failed ({exc})")


def poll_job(job_id: str, cap: int = 320) -> dict:
    """Block until the API job finishes (3s cadence, cap polls)."""
    for _ in range(cap):
        try:
            j = api(f"/api/job/{job_id}")
        except Exception:
            time.sleep(3)
            continue
        if j.get("status") in ("done", "error"):
            return j
        time.sleep(3)
    return {"status": "timeout", "error": None, "result": None}


def fmt_dur(s) -> str:
    try:
        s = int(s or 0)
        return f"{s // 60}:{s % 60:02d}"
    except (TypeError, ValueError):
        return "?:??"


_BADGES = {"jiosaavn": "[JS 320]", "youtube_music": "[YT 128]", "qbit": "[FLAC]"}


def fmt_pick(p: dict) -> str:
    src = p.get("source")
    badge = _BADGES.get(src, f"[{html.escape(str(src or '?'))}]")
    title = html.escape(p.get("title") or "?")
    artist = html.escape(p.get("artist") or "?")
    album = html.escape(p.get("album") or "")
    dur = fmt_dur(p.get("duration")) if p.get("duration") else "?:??"
    out = f"{badge} <b>{artist}</b> — {title} ({dur})"
    if album:
        out += f" · {album}"
    if src == "qbit":
        if p.get("seeders") is not None:
            out += f" · 🌱{p['seeders']}"
        if p.get("size_mb"):
            out += f" · {p['size_mb']}MB"
    return out


def _btn(p: dict, n: int) -> str:
    """Short button label for a candidate."""
    if p.get("source") == "qbit":
        s = f"{n}. [FLAC] {p.get('title') or '?'}"
        if p.get("seeders") is not None:
            s += f" · {p['seeders']}🌱"
        return s[:64]
    return f"{n}. {(p.get('artist') or '?')} — {p.get('title') or '?'}"[:64]


def _cand_key(c: dict) -> tuple[str, str]:
    """Exact key — used only to dedupe the displayed result list."""
    return ((c.get("artist") or "").lower().strip(), (c.get("title") or "").lower().strip())


_JUNK = {
    "feat", "feats", "ft", "with", "official", "audio", "video", "lyrics", "lyric",
    "hd", "hq", "flac", "mp3", "aac", "m4a", "opus", "wav", "320", "160", "128",
    "24bit", "16bit", "khz", "bit", "cd", "rip", "web", "webrip", "hdtv", "vinyl",
    "single", "album", "ep", "remaster", "remastered", "deluxe", "edition",
    "explicit", "clean", "the", "a", "an", "and", "of", "to",
}


def _tokens(s) -> set[str]:
    """Lowercased word tokens, brackets + release/codec noise removed."""
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", str(s or "").lower())
    return {w for w in re.findall(r"[a-z0-9]+", s) if len(w) > 1 and w not in _JUNK}


def _raw_tokens(s) -> set[str]:
    """Tokens from the RAW string, brackets kept — that's where variant markers
    live ("Get Lucky (Radio Edit)", "… (Daft Punk Remix)")."""
    return {w for w in re.findall(r"[a-z0-9]+", str(s or "").lower()) if len(w) > 1}


_VARIANTS = {
    "remix", "rmx", "live", "acoustic", "instrumental", "karaoke", "cover", "radio",
    "edit", "extended", "mix", "mixed", "demo", "reprise", "sped", "slowed",
    "nightcore", "orchestral", "piano", "reverb", "mashup", "bootleg", "refix",
    "flip", "8d",
}


def _artist_ok(a: dict, b: dict) -> bool:
    """Artist agreement, tolerating torrent rows (which carry no artist field):
    when one side is artistless, look for the other's artist tokens inside its
    release name — that's what keeps a cover by someone else from grouping."""
    aa, ab = _tokens(a.get("artist")), _tokens(b.get("artist"))
    if aa and ab:
        return bool(aa & ab)
    if aa:
        return bool(aa & _tokens(b.get("title")))
    if ab:
        return bool(ab & _tokens(a.get("title")))
    return True


def _same_track(a: dict, b: dict) -> bool:
    """Loose same-track test: strong title-token overlap, IDENTICAL variant
    markers (a remix/live/cover/radio-edit must never pair with the original —
    storefront picks are used verbatim, so a wrong pair would silently file the
    wrong recording), and artist agreement."""
    ta, tb = _tokens(a.get("title")), _tokens(b.get("title"))
    if not ta or not tb:
        return False
    if (_raw_tokens(a.get("title")) & _VARIANTS) != (_raw_tokens(b.get("title")) & _VARIANTS):
        return False
    inter = ta & tb
    ok = len(inter) >= 2 and len(inter) / min(len(ta), len(tb)) >= 0.5
    if not ok and len(inter) == 1 and min(len(ta), len(tb)) == 1:
        ok = True  # one-word title ("Happy") vs a long release name — that token is all we have
    if not ok:
        return False
    return _artist_ok(a, b)


def _cluster(cands: list[dict]) -> tuple[list[dict[str, dict]], dict[int, int]]:
    """Group candidates into "same track, different source" clusters.

    Returns the clusters plus an id(candidate) -> cluster index map. Shared by the
    search flow and the single-track-link flow so both offer identical options.
    """
    groups: list[dict[str, dict]] = []
    gid: dict[int, int] = {}
    for c in cands:
        if not c.get("source"):
            continue
        for i, g in enumerate(groups):
            if _same_track(c, next(iter(g.values()))):
                g.setdefault(str(c["source"]), c)
                gid[id(c)] = i
                break
        else:
            gid[id(c)] = len(groups)
            groups.append({str(c["source"]): c})
    return groups, gid


def _qbtn(c: dict) -> str:
    """Picker button label: quality badge (+ seeders for torrents)."""
    src = c.get("source")
    label = _BADGES.get(src, f"[{html.escape(str(src or '?'))}]")
    if src == "qbit" and c.get("seeders") is not None:
        label += f" {c['seeders']}🌱"
    return label


def probe(path) -> str:
    """Honest, compact quality summary of the delivered file (ffprobe)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries",
             "format=format_name,bit_rate:stream=codec_name,sample_rate,bits_per_raw_sample",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=25)
        j = json.loads(r.stdout or "{}")
    except Exception:
        return ""
    fmt = (j.get("format") or {}).get("format_name", "")
    st = (j.get("streams") or [{}])[0]
    codec = st.get("codec_name") or ""
    sr = st.get("sample_rate")
    bits = st.get("bits_per_raw_sample")
    br = (j.get("format") or {}).get("bit_rate") or st.get("bit_rate")
    try:
        br_k = f"{int(br) // 1000}k" if br else ""
    except (TypeError, ValueError):
        br_k = ""
    if "flac" in fmt:
        q = "FLAC"
        if sr:
            q += f" {float(sr) / 1000:.1f}kHz"
        if bits:
            q += f"/{bits}bit"
        return q
    if codec:
        return codec.upper() + (f" {br_k}" if br_k else "")
    return br_k or fmt or ""


def fmt_attempts(attempts) -> str:
    """Delivery trail: 'ytmusic ✘ (403) → qbit ✔ FLAC fallback'."""
    if not attempts:
        return ""
    bits = []
    for a in attempts:
        src = str(a.get("source") or "?")
        if a.get("status") == "ok":
            note = f" ({a['note']})" if a.get("note") else ""
            bits.append(f"{src} ✔{note}")
        else:
            err = str(a.get("error") or "failed").strip()
            # trim the noisy yt-dlp traceback tail to the first real message
            bits.append(f"{src} ✘ {err[:70]}")
    return " → ".join(bits)


# -- flows ----------------------------------------------------------------

def flow_search(chat: int, query: str):
    say(chat, f"🔎 searching <i>{html.escape(query)}</i>…")
    try:
        jid = api("/api/search", {"query": query})["job_id"]
    except Exception as exc:
        say(chat, f"❌ API error: {exc}")
        return
    j = poll_job(jid)
    if j["status"] != "done" or not (j.get("result") or {}).get("candidates"):
        say(chat, f"❌ no results for <i>{html.escape(query)}</i>")
        return
    cands = j["result"]["candidates"]
    # Cluster candidates into "same track, different source" groups. Matching is
    # deliberately tolerant: storefront rows are clean ("Get Lucky (feat. X)") while
    # torrent rows are release names ("Daft Punk feat. X - Get Lucky (Single) FLAC
    # 24-96") — an exact key never pairs them. A loose match is safe because qbit
    # re-verifies the track inside the pack by title+duration at download time, so
    # the worst case is a clean "no match in pack" error, never a wrong file.
    groups, gid = _cluster(cands)
    seen, n = set(), 0
    lines = ["<b>Results</b> — <i>tap a button to download</i> (or reply <code>download N</code>):"]
    store, alts, rows = {}, {}, []
    for c in cands[:15]:
        key = _cand_key(c)
        if key in seen:
            continue
        seen.add(key)
        n += 1
        store[n] = c
        alts[n] = groups[gid[id(c)]] if id(c) in gid else {str(c.get("source") or "?"): c}
        lines.append(f"{n}. {fmt_pick(c)}")
        rows.append([{"text": _btn(c, n), "callback_data": f"dl:{n}"}])
        if n >= 12:
            break
    _picks[chat] = store
    _alts[chat] = alts
    say(chat, "\n".join(lines), reply_markup=json.dumps({"inline_keyboard": rows}))


def flow_download(chat: int, n: int):
    """A tapped result: offer the qualities it's actually available in, else download."""
    pick = (_picks.get(chat) or {}).get(n)
    if not pick:
        say(chat, "❌ no search results in this chat — search first")
        return
    opts = list(((_alts.get(chat) or {}).get(n) or {}).values())
    if len(opts) < 2:  # single source — nothing to choose, don't add a tap
        _do_download(chat, pick)
        return
    art = str(pick.get("artist") or "").strip()
    tit = str(pick.get("title") or "?").strip()
    head = f"{html.escape(art)} — {html.escape(tit)}" if art and art != "?" else html.escape(tit)
    dur = f" ({fmt_dur(pick.get('duration'))})" if pick.get("duration") else ""
    rows = [[{"text": _qbtn(c), "callback_data": f"q:{n}:{c.get('source')}"} for c in opts]]
    say(chat, f"🎚 <b>Choose quality</b> — {head}{dur}",
        reply_markup=json.dumps({"inline_keyboard": rows}))


def _do_download(chat: int, pick: dict):
    q = f"{pick.get('artist', '')} {pick.get('title', '')}".strip()
    say(chat, f"⬇️ downloading {fmt_pick(pick)}…")
    try:
        jid = api("/api/download", {"query": q, "pick": pick})["job_id"]
    except Exception as exc:
        say(chat, f"❌ API error: {exc}")
        return
    j = poll_job(jid)
    res = j.get("result") or {}
    tr = fmt_attempts(res.get("attempts"))
    if j["status"] == "done" and res.get("output"):
        out = res["output"]
        qual = probe(out)
        msg = f"✅ {fmt_pick(pick)}"
        if qual:
            msg += f"\n🎧 <b>{html.escape(qual)}</b>"
        extra = []
        if "lyrics" in res:
            extra.append(f"lyrics {'yes' if res['lyrics'] else 'no'}")
        if "cover" in res:
            extra.append(f"cover {'yes' if res['cover'] else 'no'}")
        if extra:
            msg += f" · ({', '.join(extra)})"
        if tr:
            msg += f"\n↪️ {html.escape(tr)}"
        msg += f"\n<code>{html.escape(out)}</code>"
        say(chat, msg)
        try:
            if Path(out).stat().st_size < 50 * 1024 * 1024:
                send_audio(chat, out, f"{pick.get('artist')} — {pick.get('title')}")
        except OSError:
            pass
    else:
        say(chat, f"❌ {html.escape(str(j.get('error') or 'download failed'))}"
                 + (f"\n↪️ {html.escape(tr)}" if tr else ""))


def flow_link(chat: int, url: str):
    """A pasted link. A single track asks which quality; albums/playlists just download."""
    say(chat, f"🔗 resolving <i>{html.escape(url)}</i> — queued…")
    jid = None
    try:
        jid = api("/api/resolve", {"url": url})["job_id"]
    except Exception:
        jid = None
    if jid:
        j = poll_job(jid, cap=180)
        r = j.get("result") or {}
        if j.get("status") == "done" and r.get("kind") == "track" and r.get("pick"):
            offer_link_quality(chat, url, r["pick"])
            return
    _link_download(chat, url)


def offer_link_quality(chat: int, url: str, pick: dict):
    """Offer the sources that actually hold this track. The pasted link's own
    pick is always on the list, so tapping it is the old download-the-link path."""
    art = str(pick.get("artist") or "").strip()
    tit = str(pick.get("title") or "").strip()
    q = f"{art} {tit}".strip()
    opts: dict[str, dict] = {}
    if q:
        say(chat, "🔎 checking which sources have this track…")
        try:
            jid = api("/api/search", {"query": q})["job_id"]
            j = poll_job(jid, cap=120)
            cands = (j.get("result") or {}).get("candidates") or []
        except Exception:
            cands = []
        groups, _ = _cluster(cands)
        hit = next((g for g in groups if any(_same_track(c, pick) for c in g.values())), None)
        opts = dict(hit or {})
    opts[str(pick.get("source") or "link")] = pick  # the link's own source wins
    if len(opts) < 2:  # nothing to choose — download the link as before
        _link_download(chat, url)
        return
    _alts[chat] = {**_alts.get(chat, {}), 0: opts}  # slot 0 = "this pasted link"
    head = f"{html.escape(art)} — {html.escape(tit)}" if art else html.escape(tit or "track")
    dur = f" ({fmt_dur(pick.get('duration'))})" if pick.get("duration") else ""
    rows = [[{"text": _qbtn(c), "callback_data": f"q:0:{s}"} for s, c in opts.items()]]
    say(chat, f"🎚 <b>Choose quality</b> — {head}{dur}",
        reply_markup=json.dumps({"inline_keyboard": rows}))


def _link_download(chat: int, url: str):
    try:
        jid = api("/api/playlist", {"url": url})["job_id"]
    except Exception as exc:
        say(chat, f"❌ API error: {exc}")
        return
    j = poll_job(jid, cap=600)
    res = j.get("result") or {}
    if j["status"] != "done":
        say(chat, f"❌ {html.escape(str(j.get('error') or 'job failed'))}")
        return
    # single-track result shape: {query, picked, output, format, meta, lyrics, cover}
    if res.get("output") and "outputs" not in res:
        picked = res.get("picked") or {}
        art = html.escape(str(picked.get("artist") or ""))
        tit = html.escape(str(picked.get("title") or Path(res["output"]).stem))
        out = res["output"]
        extra = []
        if "lyrics" in res:
            extra.append(f"lyrics {'yes' if res['lyrics'] else 'no'}")
        if "cover" in res:
            extra.append(f"cover {'yes' if res['cover'] else 'no'}")
        msg = f"✅ <b>{art} — {tit}</b>" + (f" ({', '.join(extra)})" if extra else "")
        qual = probe(out)
        tra = fmt_attempts(res.get("attempts"))
        if qual:
            msg += f" · 🎧 <b>{html.escape(qual)}</b>"
        if tra:
            msg += f"\n↪️ {html.escape(tra)}"
        msg += f"\n<code>{html.escape(out)}</code>"
        say(chat, msg)
        try:
            if Path(out).stat().st_size < 50 * 1024 * 1024:
                send_audio(chat, out, f"{art} — {tit}")
        except OSError:
            pass
        return
    # album/playlist result shape: {name, total, downloaded, errors, outputs}
    errs = res.get("errors") or []
    name = html.escape(str(res.get("name") or "playlist"))
    total, dl = res.get("total", 0), res.get("downloaded", 0)
    head = f"✅ <b>{name}</b> — {dl}/{total} tracks"
    outs = res.get("outputs") or []
    if errs:
        head += f"\n⚠️ {len(errs)} failed:"
        for e in errs[:5]:
            head += f"\n• {html.escape(str(e.get('track') or e))[:120]}"
        if len(errs) > 5:
            head += f"\n… +{len(errs) - 5} more"
    say(chat, head)
    if len(outs) == 1 and outs[0].get("output"):
        p = outs[0]["output"]
        try:
            if Path(p).stat().st_size < 50 * 1024 * 1024:
                send_audio(chat, p, (res.get("name") or Path(p).stem))
        except OSError:
            pass


def flow_status(chat: int):
    try:
        jobs = api("/api/jobs")
    except Exception as exc:
        say(chat, f"❌ API error: {exc}")
        return
    if not jobs:
        say(chat, "🫥 queue is empty")
        return
    by = {}
    for j in jobs:
        by[j.get("status", "?")] = by.get(j.get("status", "?"), 0) + 1
    cur = next((j for j in jobs if j.get("status") == "running"), None)
    msg = "📊 queue: " + " · ".join(f"{k}={v}" for k, v in sorted(by.items()))
    if cur:
        pl = cur.get("payload") or {}
        msg += f"\n▶️ running: {pl.get('kind')} {pl.get('url') or pl.get('query') or ''}"
    say(chat, html.escape(msg))


HELP = (
    "<b>MusicFetch</b> 🎵\n\n"
    "• Paste a YouTube / YouTube Music / Spotify link — a single track asks which "
    "quality first; an album/playlist downloads everything.\n"
    "• <code>search &lt;query&gt;</code> — find tracks\n"
    "• <code>download N</code> — grab result N from the last search\n"
    "• tapping a result shows its available qualities (FLAC / 320 / 128) — pick one\n"
    "• <code>/status</code> — queue state\n"
    "• <code>/help</code> — this\n\n"
    "Files land in the Jellyfin library; single tracks are also sent here as audio."
)


def handle(chat: int, text: str):
    t = (text or "").strip()
    low = t.lower()
    if low in ("/start", "/help", "help", "start"):
        say(chat, HELP)
        return
    if low == "/status" or low == "status":
        flow_status(chat)
        return
    m = re.match(r"^(?:download|dl)\s+(\d+)$", low)
    if m:
        flow_download(chat, int(m.group(1)))
        return
    m = re.match(r"^search\s+(.+)$", t, re.S)
    if m:
        flow_search(chat, m.group(1).strip())
        return
    m = _LINK_RE.search(t)
    if m:
        flow_link(chat, m.group(0).rstrip(".,)"))
        return
    say(chat, HELP)


def main():
    me = tg("getMe")
    log.info("bot online: @%s (%s)", me["result"]["username"], me["result"]["id"])
    offset = 0
    while True:
        try:
            upd = tg("getUpdates", offset=offset, timeout=25,
                     allowed_updates=["message", "callback_query"])
        except Exception as exc:
            log.warning("getUpdates error: %s", exc)
            time.sleep(3)
            continue
        for u in upd.get("result", []):
            offset = max(offset, u["update_id"] + 1)
            if "callback_query" in u:
                cq = u["callback_query"]
                chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
                cid = cq.get("id")
                if cid:
                    try:
                        tg("answerCallbackQuery", callback_query_id=cid)
                    except Exception:
                        pass
                if chat_id is None:
                    continue
                if chat_id not in ALLOWED:
                    say(chat_id, "❌ Not authorized.")
                    continue
                m = re.match(r"^dl:(\d+)$", cq.get("data") or "")
                if m:
                    threading.Thread(target=flow_download,
                                      args=(chat_id, int(m.group(1))),
                                      daemon=True).start()
                    continue
                m = re.match(r"^q:(\d+):([a-z_]+)$", cq.get("data") or "")
                if m:
                    cand = (((_alts.get(chat_id) or {}).get(int(m.group(1))) or {})
                            .get(m.group(2)))
                    if not cand:
                        say(chat_id, "❌ that choice expired — search again")
                        continue
                    threading.Thread(target=_do_download, args=(chat_id, cand),
                                     daemon=True).start()
                continue
            msg = u.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text") or ""
            if chat_id is None:
                continue
            if chat_id not in ALLOWED:
                log.warning("ignoring unauthorized chat %s: %r", chat_id, text[:80])
                say(chat_id, "❌ Not authorized.")
                continue
            threading.Thread(target=handle, args=(chat_id, text), daemon=True).start()


if __name__ == "__main__":
    main()
