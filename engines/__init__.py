"""Engine registry for MusicFetch. Engines are source-pluggable:
jiosaavn (primary, 320kbps AAC) and youtube_music (fallback, ~128kbps AAC).
cfg['engine']['order'] controls search merge order and download routing.
"""

import logging

logger = logging.getLogger(__name__)


class EngineError(Exception):
    """Base for all engine failures (search / download / metadata)."""


def _build(cfg: dict, name: str):
    if name == "jiosaavn":
        from .jiosaavn import JioSaavnEngine

        return JioSaavnEngine(cfg)
    if name == "youtube_music":
        from .youtube_music import YoutubeMusicEngine

        return YoutubeMusicEngine(cfg)
    raise EngineError(f"unknown engine: {name}")


class MultiEngine:
    """Search merged across engine.order; download/meta route by pick['source']."""

    def __init__(self, engines: list):
        self.engines = engines

    def search(self, query: str, limit: int = 10) -> list[dict]:
        out, seen = [], set()
        for eng in self.engines:
            try:
                for c in eng.search(query, limit):
                    key = (c.get("source"), c.get("track_id"))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(c)
            except Exception as exc:
                logger.warning("engine %s search failed: %s", type(eng).__name__, exc)
        return out

    def download(self, pick: dict):
        return self._for(pick).download(pick)

    def meta(self, local, pick: dict, query: str) -> dict:
        return self._for(pick).meta(local, pick, query)

    def _for(self, pick: dict):
        src = pick.get("source")
        for eng in self.engines:
            if getattr(eng, "name", None) == src:
                return eng
        raise EngineError(f"no engine registered for source: {src!r}")


def get_engine(cfg: dict):
    """Return engine(s) per cfg['engine']['order'] — a MultiEngine when more
    than one engine is configured."""
    order = cfg.get("engine", {}).get("order") or []
    if not order:
        raise EngineError("no engines configured (engine.order is empty)")
    engines = [_build(cfg, name) for name in order]
    return engines[0] if len(engines) == 1 else MultiEngine(engines)
