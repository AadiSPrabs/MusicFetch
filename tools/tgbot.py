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
    seen, n = set(), 0
    lines = ["<b>Results</b> — <i>tap a button to download</i> (or reply <code>download N</code>):"]
    store = {}
    rows = []
    for c in j["result"]["candidates"][:15]:
        key = ((c.get("artist") or "").lower(), (c.get("title") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        n += 1
        store[n] = c
        lines.append(f"{n}. {fmt_pick(c)}")
        rows.append([{"text": _btn(c, n), "callback_data": f"dl:{n}"}])
        if n >= 12:
            break
    _picks[chat] = store
    say(chat, "\n".join(lines), reply_markup=json.dumps({"inline_keyboard": rows}))


def flow_download(chat: int, n: int):
    pick = (_picks.get(chat) or {}).get(n)
    if not pick:
        say(chat, "❌ no search results in this chat — search first")
        return
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
    say(chat, f"🔗 resolving <i>{html.escape(url)}</i> — queued…")
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
    "• Paste a YouTube / YouTube Music / Spotify link (track, album or playlist) — "
    "I resolve and download everything.\n"
    "• <code>search &lt;query&gt;</code> — find tracks\n"
    "• <code>download N</code> — grab result N from the last search\n"
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
