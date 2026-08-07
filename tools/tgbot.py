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


def fmt_pick(p: dict) -> str:
    badge = "[JS]" if p.get("source") == "jiosaavn" else "[YT]"
    title = html.escape(p.get("title") or "?")
    artist = html.escape(p.get("artist") or "?")
    album = html.escape(p.get("album") or "")
    return f"{badge} <b>{artist}</b> — {title} ({fmt_dur(p.get('duration'))})" + (f" · {album}" if album else "")


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
    lines = ["<b>Results</b> — reply <code>download N</code> to grab one:"]
    store = {}
    for c in j["result"]["candidates"][:15]:
        key = ((c.get("artist") or "").lower(), (c.get("title") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        n += 1
        store[n] = c
        lines.append(f"{n}. {fmt_pick(c)}")
        if n >= 10:
            break
    _picks[chat] = store
    say(chat, "\n".join(lines))


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
    if j["status"] == "done" and res.get("output"):
        out = res["output"]
        say(chat, f"✅ saved → <code>{html.escape(out)}</code>"
                  + (f"\n🎤 lyrics: {'yes' if res.get('lyrics') else 'no'} · 🖼 cover: {'yes' if res.get('cover') else 'no'}"
                     if "lyrics" in res else ""))
        try:
            if Path(out).stat().st_size < 50 * 1024 * 1024:
                send_audio(chat, out, f"{pick.get('artist')} — {pick.get('title')}")
        except OSError:
            pass
    else:
        say(chat, f"❌ {html.escape(str(j.get('error') or 'download failed'))}")


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
            upd = tg("getUpdates", offset=offset, timeout=25, allowed_updates=["message"])
        except Exception as exc:
            log.warning("getUpdates error: %s", exc)
            time.sleep(3)
            continue
        for u in upd.get("result", []):
            offset = max(offset, u["update_id"] + 1)
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
