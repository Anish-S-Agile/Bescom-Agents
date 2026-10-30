"""
Monitoring events for Grafana: LLM calls, agent runs, sent messages and HTTP requests.

Events are queued in memory and written in batches to the `metrics_*` tables of the agent database by a
background thread, so recording never blocks a request and never raises. Grafana reads those tables with its
PostgreSQL data source (see grafana/ and docker-compose.postgres.yml).

    from app.services.metrics_recorder import metrics
    metrics.track_message("email", "sent")

    METRICS_ENABLED          true (default) / false
    METRICS_RETENTION_DAYS   delete events older than this, default 30
"""
import logging
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_FLUSH_INTERVAL = 2.0
_MAX_BATCH = 500
_PURGE_EVERY = 3600.0


class MetricsRecorder:
    def __init__(self, store=None, max_queue: int = 10000) -> None:
        self._store = store
        self.enabled = os.getenv("METRICS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
        self.retention_days = int(os.getenv("METRICS_RETENTION_DAYS", "30"))
        self._q: "queue.Queue[Tuple[str, Dict[str, Any]]]" = queue.Queue(maxsize=max_queue)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_purge = 0.0

    @property
    def store(self):
        if self._store is None:
            from app.services.agent_store import agent_store
            return agent_store
        return self._store

    @store.setter
    def store(self, value) -> None:
        self._store = value

    # ------------------------------------------------------------------ recording
    def _emit(self, kind: str, **fields: Any) -> None:
        if not self.enabled:
            return
        try:
            self._q.put_nowait((kind, fields))
        except queue.Full:  # monitoring is best-effort: drop rather than grow without bound
            return
        self._ensure_worker()

    def track_llm_call(self, model: str, operation: str, input_tokens: int = 0, output_tokens: int = 0,
                       duration: float = 0.0, success: bool = True, error_type: Optional[str] = None) -> None:
        self._emit("llm_call", model=model, operation=operation,
                   status="success" if success else (error_type or "error"), duration_ms=duration * 1000,
                   input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0))

    def track_agent_run(self, agent_name: str, operation: str, duration: float, success: bool) -> None:
        self._emit("agent_run", agent_name=agent_name, operation=operation, success=bool(success),
                   duration_ms=duration * 1000)

    def track_message(self, channel: str, status: str) -> None:
        self._emit("message", channel=channel, status=status)

    def track_http(self, method: str, route: str, status_code: int, duration: float) -> None:
        self._emit("http_request", method=method[:8], route=route[:200], status_code=int(status_code),
                   duration_ms=duration * 1000)

    # ------------------------------------------------------------------ background writer
    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="metrics-writer", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            batch: List[Tuple[str, Dict[str, Any]]] = []
            try:
                batch.append(self._q.get(timeout=_FLUSH_INTERVAL))
                while len(batch) < _MAX_BATCH:
                    batch.append(self._q.get_nowait())
            except queue.Empty:
                pass
            if batch:
                try:
                    if self.store.ready:  # never initialise the database as a side effect of monitoring
                        self.store.record_metrics(batch)
                except Exception as e:  # database down / tables missing: drop the batch, keep the app alive
                    logger.debug("metrics batch dropped: %s", e)
                for _ in batch:
                    self._q.task_done()
            self._maybe_purge()

    def _maybe_purge(self) -> None:
        if self.retention_days <= 0 or time.monotonic() - self._last_purge < _PURGE_EVERY:
            return
        if not self.store.ready:  # never initialise the database as a side effect of monitoring
            return
        self._last_purge = time.monotonic()
        try:
            self.store.purge_metrics(self.retention_days)
        except Exception as e:
            logger.debug("metrics purge failed: %s", e)

    def flush(self, timeout: float = 10.0) -> None:
        """Block until queued events are written (tests, shutdown)."""
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            self._ensure_worker()  # the writer may have been stopped by shutdown() while events were queued
            time.sleep(0.02)

    def shutdown(self) -> None:
        """Write what is queued, then stop the writer (it restarts on the next event)."""
        self.flush()
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_FLUSH_INTERVAL + 1)


metrics = MetricsRecorder()
