"""In-process job queue for MusicFetch. Serial execution (slskd allows one
concurrent search; downloads run one at a time for v1 politeness)."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable


class Queue:
    def __init__(self, worker: Callable[[dict], None]):
        self.worker = worker
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._busy = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def submit(self, payload: dict) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created": time.time(),
                "updated": time.time(),
                "payload": payload,
                "result": None,
                "error": None,
            }
            self._cond.notify()
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(job_id)
            return dict(j) if j else None

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs.values()]

    def _loop(self):
        while True:
            with self._cond:
                # Block until a *queued* job exists. Jobs are never removed
                # from _jobs (they persist as done/error), so the wait must
                # key on queued-status, not on _jobs being empty — otherwise
                # the loop spins at 100% CPU once the first job completes.
                job = next((j for j in self._jobs.values() if j["status"] == "queued"), None)
                while job is None:
                    self._busy = False
                    self._cond.wait(timeout=60)
                    job = next((j for j in self._jobs.values() if j["status"] == "queued"), None)
                self._busy = True
                job["status"] = "running"
            try:
                result = self.worker(job["payload"])
                with self._lock:
                    job["result"] = result
                    job["status"] = "done"
            except Exception as exc:  # noqa: BLE001 — job must never kill the loop
                with self._lock:
                    job["error"] = str(exc)
                    job["status"] = "error"
            finally:
                with self._lock:
                    job["updated"] = time.time()
                    self._busy = False
                    self._cond.notify()
