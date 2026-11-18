"""Durable, best-effort bridge from Python runtimes to the local observation hub."""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import httpx

log = logging.getLogger("guanjia.observability")
_ROOT = Path(__file__).resolve().parents[2]
_SENSITIVE = ("authorization", "cookie", "api_key", "apikey", "access_token",
              "refresh_token", "credential", "password", "secret")


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if any(part in str(key).lower().replace("-", "_") for part in _SENSITIVE)
            else _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _runtime_url() -> str:
    configured = os.environ.get("GUANJIA_OBSERVABILITY_URL")
    if configured:
        return configured.rstrip("/")
    brain_url = os.environ.get("GUANJIA_BRAIN_URL", "http://127.0.0.1:18800/v1/chat/completions")
    return brain_url.split("/v1/", 1)[0].rstrip("/") + "/internal/observability/events"


def _token() -> str:
    token = os.environ.get("GUANJIA_GATEWAY_TOKEN") or os.environ.get("GUANJIA_BRIDGE_TOKEN")
    if token:
        return token
    config = _ROOT / "agent-runtime" / "config" / "runtime.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        value = data.get("token")
        return value if isinstance(value, str) else ""
    except (OSError, ValueError):
        return ""


class ObservationClient:
    """A bounded SQLite outbox with retrying at-least-once delivery."""

    def __init__(
        self,
        *,
        database_path: str | Path | None = None,
        endpoint: str | None = None,
        token: str | None = None,
        sender: Callable[[list[dict[str, Any]]], bool] | None = None,
        auto_start: bool = True,
        max_events: int = 20_000,
    ) -> None:
        self.database_path = Path(database_path or os.environ.get(
            "GUANJIA_OBSERVABILITY_OUTBOX",
            _ROOT / "data" / "agent-runtime" / "python-observability.sqlite",
        ))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.endpoint = endpoint or _runtime_url()
        self.token = token if token is not None else _token()
        self.sender = sender
        self.max_events = max_events
        self.producer_id = f"python:{os.getpid()}:{uuid4().hex}"
        self.instance_prefix = f"{socket.gethostname()}:{os.getpid()}"
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._db = sqlite3.connect(self.database_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS observation_outbox (
                event_id TEXT PRIMARY KEY,
                producer_sequence INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS observation_meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
        """)
        self._db.execute(
            "INSERT OR IGNORE INTO observation_meta(key, value) VALUES ('sequence', 0)"
        )
        self._db.commit()
        self._http = httpx.Client(timeout=httpx.Timeout(2.0, connect=1.0), trust_env=False)
        self._thread: threading.Thread | None = None
        for node_id, name, kind, zone in (
            ("voice-runtime", "语音运行时", "processor", "INPUT"),
            ("audio-capture", "音频采集", "processor", "INPUT"),
            ("vad", "语音活动检测", "processor", "INPUT"),
            ("asr", "语音识别", "processor", "INPUT"),
            ("speaker-recognition", "说话人识别", "processor", "INPUT"),
            ("tts", "语音合成", "processor", "OUTPUT"),
            ("audio-playback", "音频播放", "processor", "OUTPUT"),
        ):
            self._record("node.registered", node_id=node_id, payload={
                "name": name, "type": kind, "zone": zone,
            })
            self._record("node.heartbeat", node_id=node_id,
                         instance_id=f"{self.instance_prefix}:{node_id}", payload={
                             "instanceId": f"{self.instance_prefix}:{node_id}",
                             "processId": os.getpid(),
                             "hostname": socket.gethostname(),
                             "health": "online",
                         })
        if auto_start:
            self._thread = threading.Thread(target=self._run, name="observability-outbox", daemon=True)
            self._thread.start()

    def start_trace(
        self,
        session_id: str,
        user_text: str,
        model: str = "voice",
        *,
        started_at: str | None = None,
        started_clock: float | None = None,
    ) -> Trace:
        trace = Trace(self, uuid4().hex, session_id, started_at or _now(),
                      started_clock if started_clock is not None else time.monotonic(), user_text, model)
        self._record("trace.started", trace_id=trace.trace_id, node_id="voice-runtime",
                     timestamp=trace.started_at,
                     payload={"sessionId": session_id, "input": {
                         "sessionId": session_id, "userText": user_text, "model": model,
                     }})
        return trace

    def flush_once(self, limit: int = 100) -> int:
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT event_id, event_json FROM observation_outbox "
                    "ORDER BY producer_sequence LIMIT ?", (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            log.error("observability outbox read failed: %s", exc)
            return 0
        if not rows:
            return 0
        events = [json.loads(row[1]) for row in rows]
        try:
            if self.sender is not None:
                delivered = self.sender(events)
            elif self.token:
                response = self._http.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={"events": events},
                )
                response.raise_for_status()
                delivered = response.json().get("accepted") == len(events)
            else:
                return 0
        except (httpx.HTTPError, ValueError, OSError) as exc:
            log.warning("observability delivery deferred: %s", exc)
            return 0
        if not delivered:
            return 0
        try:
            with self._lock:
                self._db.executemany(
                    "DELETE FROM observation_outbox WHERE event_id = ?",
                    [(row[0],) for row in rows],
                )
                self._db.commit()
        except sqlite3.Error as exc:
            log.error("observability outbox acknowledgement failed: %s", exc)
            return 0
        return len(rows)

    def close(self, timeout: float = 2.0) -> None:
        for node_id in ("voice-runtime", "audio-capture", "asr", "tts", "audio-playback"):
            instance_id = f"{self.instance_prefix}:{node_id}"
            self._record("node.health_changed", node_id=node_id, instance_id=instance_id,
                         payload={"instanceId": instance_id, "health": "offline"})
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline and self.flush_once():
            pass
        with self._lock:
            self._db.close()
        self._http.close()

    def _record(
        self,
        event_type: str,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        node_id: str | None = None,
        instance_id: str | None = None,
        payload: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            with self._lock:
                count = self._db.execute("SELECT count(*) FROM observation_outbox").fetchone()[0]
                if count >= self.max_events:
                    log.error("observability outbox full; event not recorded: %s", event_type)
                    return None
                self._db.execute(
                    "UPDATE observation_meta SET value = value + 1 WHERE key = 'sequence'"
                )
                sequence = self._db.execute(
                    "SELECT value FROM observation_meta WHERE key = 'sequence'"
                ).fetchone()[0]
                event: dict[str, Any] = {
                    "eventId": uuid4().hex,
                    "producerId": self.producer_id,
                    "producerSequence": sequence,
                    "eventType": event_type,
                    "timestamp": timestamp or _now(),
                    "payload": _safe(payload or {}),
                }
                if trace_id:
                    event["traceId"] = trace_id
                if span_id:
                    event["spanId"] = span_id
                if parent_span_id:
                    event["parentSpanId"] = parent_span_id
                if node_id:
                    event["nodeId"] = node_id
                if instance_id:
                    event["instanceId"] = instance_id
                self._db.execute(
                    "INSERT INTO observation_outbox(event_id, producer_sequence, event_json, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (event["eventId"], sequence, json.dumps(event, ensure_ascii=False), event["timestamp"]),
                )
                self._db.commit()
        except (sqlite3.Error, TypeError, ValueError) as exc:
            log.error("observability event could not be persisted (%s): %s", event_type, exc)
            return None
        self._wake.set()
        return event

    def _run(self) -> None:
        delay = 0.1
        heartbeat_at = time.monotonic()
        while not self._stop.is_set():
            delivered = self.flush_once()
            now = time.monotonic()
            if now - heartbeat_at >= 20:
                for node_id in ("voice-runtime", "audio-capture", "asr", "tts", "audio-playback"):
                    instance_id = f"{self.instance_prefix}:{node_id}"
                    self._record("node.heartbeat", node_id=node_id, instance_id=instance_id,
                                 payload={"instanceId": instance_id, "processId": os.getpid(),
                                          "hostname": socket.gethostname(), "health": "online"})
                heartbeat_at = now
            delay = 0.1 if delivered else min(5.0, delay * 2)
            self._wake.wait(delay)
            self._wake.clear()


@dataclass
class Span:
    client: ObservationClient
    trace_id: str
    span_id: str
    parent_span_id: str | None
    node_id: str
    name: str
    started_at: str
    started_clock: float
    ended: bool = False

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"

    def finish(
        self,
        status: str = "completed",
        output: Any = None,
        error: str | None = None,
        *,
        ended_at: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if self.ended:
            return
        self.ended = True
        final_status = status if status in {"completed", "failed", "waiting"} else "failed"
        self.client._record(
            "span.failed" if final_status == "failed" else
            "span.waiting" if final_status == "waiting" else "span.completed",
            trace_id=self.trace_id, span_id=self.span_id, parent_span_id=self.parent_span_id,
            node_id=self.node_id,
            timestamp=ended_at,
            payload={
                "name": self.name, "status": final_status,
                "durationMs": duration_ms if duration_ms is not None
                else max(0, round((time.monotonic() - self.started_clock) * 1000)),
                "output": output, "error": error,
            },
        )


@dataclass
class Trace:
    client: ObservationClient
    trace_id: str
    session_id: str
    started_at: str
    started_clock: float
    user_text: str
    model: str
    finished: bool = False
    root_span_id: str | None = None

    def start_span(
        self,
        node_id: str,
        name: str,
        input: Any,
        parent_span_id: str | None = None,
        *,
        started_at: str | None = None,
        started_clock: float | None = None,
    ) -> Span:
        span_id = uuid4().hex[:16]
        started = started_at or _now()
        parent_id = parent_span_id if parent_span_id is not None else self.root_span_id
        self.client._record(
            "span.started", trace_id=self.trace_id, span_id=span_id,
            parent_span_id=parent_id, node_id=node_id,
            payload={"name": name, "input": input}, timestamp=started,
        )
        if self.root_span_id is None:
            self.root_span_id = span_id
        return Span(self.client, self.trace_id, span_id, parent_id, node_id, name,
                    started, started_clock if started_clock is not None else time.monotonic())

    def finish(self, status: str = "completed", output: Any = None, error: str | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        final_status = status if status in {"completed", "failed"} else "failed"
        self.client._record(
            "trace.failed" if final_status == "failed" else "trace.completed",
            trace_id=self.trace_id, node_id="voice-runtime",
            payload={
                "status": final_status,
                "durationMs": max(0, round((time.monotonic() - self.started_clock) * 1000)),
                "output": output, "error": error,
            },
        )
