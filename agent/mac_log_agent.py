"""
agent/mac_log_agent.py
──────────────────────────────────────────────────────────────────────────────
macOS Unified Log collector — replaces LogCollectorAgent.cs on Windows.

Uses `log stream --style json` (Apple Unified Logging System) to receive
events in real-time, batches them, and POSTs to the FastAPI /ingest endpoint.

Usage:
    python agent/mac_log_agent.py [--url http://localhost:8000] [--level debug]

Requires macOS 10.12+ (Sierra). Run with sudo for kernel/security subsystems.
──────────────────────────────────────────────────────────────────────────────
"""

import argparse
import json
import os
import subprocess
import sys
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional
import requests

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mac_log_agent")

# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_URL      = os.environ.get("PIPELINE_URL", "http://localhost:8000")
FLUSH_INTERVAL   = 2.0      # seconds — flush buffer every N seconds
MAX_BATCH_SIZE   = 50       # flush early when buffer hits this
RETRY_ATTEMPTS   = 3
RETRY_DELAY      = 1.0      # seconds between retries

# macOS messageType → Windows-compatible severity number mapping
MSG_TYPE_TO_LEVEL = {
    "Default":     4,   # Information
    "Info":        4,   # Information
    "Debug":       5,   # Verbose
    "Error":       2,   # Error
    "Fault":       1,   # Critical
}

MSG_TYPE_TO_SEVERITY = {
    "Default":     9,
    "Info":        9,
    "Debug":       5,
    "Error":       17,
    "Fault":       21,
}


# ── Event normaliser ──────────────────────────────────────────────────────────
def normalise_event(raw: dict) -> Optional[dict]:
    """Convert a raw macOS ULS JSON event into the pipeline's StructuredLogEvent schema."""
    try:
        msg_type  = raw.get("messageType", "Default")
        subsystem = raw.get("subsystem") or raw.get("category") or "unknown"
        process   = raw.get("processImagePath", "")
        proc_name = process.split("/")[-1] if process else "unknown"

        # Derive a stable pseudo-EventId from subsystem+category hash
        category   = raw.get("category", "default")
        event_id   = abs(hash(f"{subsystem}:{category}")) % 65535

        # Parse timestamp
        ts_str = raw.get("timestamp", "")
        try:
            # macOS format: "2024-01-15 10:23:45.123456-0800"
            ts_str_clean = ts_str.rsplit("-", 1)[0].rsplit("+", 1)[0].strip()
            ts_dt = datetime.strptime(ts_str_clean[:26], "%Y-%m-%d %H:%M:%S.%f")
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            ts_unix_ms = int(ts_dt.timestamp() * 1000)
        except Exception:
            ts_dt      = datetime.now(timezone.utc)
            ts_unix_ms = int(time.time() * 1000)

        description = raw.get("eventMessage", "")

        return {
            "EventId":        event_id,
            "RecordId":       raw.get("traceID", 0),
            "Channel":        subsystem,
            "Provider":       proc_name,
            "Computer":       raw.get("machineID", "localhost"),
            "Level":          MSG_TYPE_TO_LEVEL.get(msg_type, 4),
            "LevelName":      msg_type,
            "SeverityNumber": MSG_TYPE_TO_SEVERITY.get(msg_type, 9),
            "TimestampUtc":   ts_dt.isoformat(),
            "TimestampUnix":  ts_unix_ms,
            "ProcessId":      raw.get("processID", 0),
            "ThreadId":       raw.get("threadID", 0),
            "UserId":         "",
            "Description":    description,
            "TaskCategory":   0,
            "Keywords":       [],
            "HasDescription": bool(description),
            "DescriptionLen": len(description),
            "RawXml":         "",
            # macOS-specific extras (ignored by pipeline, useful for debugging)
            "_mac_subsystem": subsystem,
            "_mac_category":  category,
            "_mac_process":   proc_name,
        }
    except Exception as exc:
        log.debug("Failed to normalise event: %s — %s", exc, raw)
        return None


# ── Flush worker ──────────────────────────────────────────────────────────────
class EventBuffer:
    """Thread-safe micro-batch buffer with timer + size-based flushing."""

    def __init__(self, pipeline_url: str):
        self._events: list[dict] = []
        self._lock              = threading.Lock()
        self._pipeline_url      = pipeline_url.rstrip("/")
        self._session           = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        self._timer: Optional[threading.Timer] = None
        self._schedule_flush()
        self.total_sent    = 0
        self.total_dropped = 0

    def add(self, event: dict):
        with self._lock:
            self._events.append(event)
            if len(self._events) >= MAX_BATCH_SIZE:
                self._flush_locked()

    def _schedule_flush(self):
        self._timer = threading.Timer(FLUSH_INTERVAL, self._timer_flush)
        self._timer.daemon = True
        self._timer.start()

    def _timer_flush(self):
        with self._lock:
            self._flush_locked()
        self._schedule_flush()

    def _flush_locked(self):
        if not self._events:
            return
        batch = self._events[:]
        self._events.clear()

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                resp = self._session.post(
                    f"{self._pipeline_url}/ingest",
                    json={"events": batch},
                    timeout=5,
                )
                if resp.status_code == 202:
                    self.total_sent += len(batch)
                    log.info("Flushed %d events (total sent: %d)", len(batch), self.total_sent)
                    return
                log.warning("Ingest returned %d (attempt %d)", resp.status_code, attempt)
            except requests.RequestException as exc:
                log.warning("POST failed (attempt %d): %s", attempt, exc)
            time.sleep(RETRY_DELAY)

        self.total_dropped += len(batch)
        log.error("Dropped %d events after %d retries", len(batch), RETRY_ATTEMPTS)

    def shutdown(self):
        if self._timer:
            self._timer.cancel()
        with self._lock:
            self._flush_locked()


# ── Main collector ────────────────────────────────────────────────────────────
def stream_logs(pipeline_url: str, level: str, predicate: Optional[str]):
    """Spawn `log stream` and feed events into the buffer."""
    cmd = ["log", "stream", "--style", "json", "--level", level]
    if predicate:
        cmd += ["--predicate", predicate]

    log.info("Starting macOS ULS collector → %s", pipeline_url)
    log.info("Command: %s", " ".join(cmd))

    buffer = EventBuffer(pipeline_url)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        log.info("Subscribed to Unified Log stream (PID %d)", proc.pid)
        log.info("Waiting for first log line… (this can take 5-10s)")

        json_buffer = ""
        parsed_count = 0
        first_line_seen = False
        decoder = json.JSONDecoder()

        for line in proc.stdout:
            if not first_line_seen and line.strip():
                first_line_seen = True
                log.info("First raw line received (len=%d): %s", len(line), line.strip()[:120])

            # Append the raw line with a space to safely separate tokens
            json_buffer += line + " "

            # Loop to extract as many complete JSON objects as possible from the current buffer
            while True:
                # Aggressively strip leading array brackets, commas, and whitespace
                json_buffer = json_buffer.lstrip(" \t\n\r,[]")
                
                if not json_buffer:
                    break

                try:
                    # raw_decode grabs the first valid JSON object and returns it + the stopping index
                    raw, index = decoder.raw_decode(json_buffer)
                    
                    # Slice off the part of the string we just successfully parsed
                    json_buffer = json_buffer[index:]

                    if not isinstance(raw, dict):
                        continue

                    parsed_count += 1
                    if parsed_count % 100 == 0:
                        log.info("Parsed %d events so far...", parsed_count)

                    event = normalise_event(raw)
                    if event:
                        buffer.add(event)

                except json.JSONDecodeError:
                    # The buffer contains an incomplete object; break and wait for more lines
                    break

    except KeyboardInterrupt:
        log.info("Interrupted — flushing remaining events…")
    finally:
        buffer.shutdown()
        if proc.poll() is None:
            proc.terminate()
        log.info(
            "Agent stopped. Sent: %d | Dropped: %d",
            buffer.total_sent,
            buffer.total_dropped,
        )


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="macOS ULS → LogSentinel agent")
    parser.add_argument(
        "--url", default=DEFAULT_URL,
        help="FastAPI backend URL (default: %(default)s)"
    )
    parser.add_argument(
        "--level", default="info",
        choices=["default", "info", "debug"],
        help="Minimum log level to capture (default: info)"
    )
    parser.add_argument(
        "--predicate", default=None,
        help='Optional NSPredicate filter, e.g. \'subsystem == "com.apple.kernel"\''
    )
    args = parser.parse_args()

    # Check API is reachable before starting stream
    try:
        r = requests.get(f"{args.url}/health", timeout=5)
        if r.status_code == 200:
            log.info("API health check passed ✓")
        else:
            log.warning("API returned %d — proceeding anyway", r.status_code)
    except requests.RequestException as exc:
        log.error("Cannot reach API at %s: %s", args.url, exc)
        log.error("Start the FastAPI backend first: uvicorn api.main:app --port 8000")
        sys.exit(1)

    stream_logs(args.url, args.level, args.predicate)


if __name__ == "__main__":
    main()