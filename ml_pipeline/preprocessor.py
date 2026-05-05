"""
ml_pipeline/preprocessor.py
──────────────────────────────────────────────────────────────────────────────
Structured feature extraction from normalised log events.

Accepts the shared StructuredLogEvent schema produced by both the Windows C#
agent and the macOS Python agent — no platform-specific code here.

Feature vector (~58 dimensions):
    Numeric  (13)  — event_id, level, severity, proc_id, channel, provider
                     hash, desc_len, has_desc, circular hour/weekday
    Window   (6)   — count + error_rate + event_rate over last 10 and 50 events
    Text     (32)  — HashingVectorizer on cleaned description (uni+bigram)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

# ── Text normalisation patterns ───────────────────────────────────────────────
_IP_RE    = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HEX_RE   = re.compile(r"0x[0-9a-fA-F]+")
_PATH_RE  = re.compile(r"(?:/[\w.\-]+){2,}")         # unix paths
_WIN_PATH = re.compile(r"[A-Za-z]:\\(?:[\w\s.\-\\]+)")
_NUM_RE   = re.compile(r"\b\d+\b")
_UUID_RE  = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Known channel ordinals (macOS subsystems map onto the same ordinal space)
_CHANNEL_ORD = {
    "system":      0,
    "application": 1,
    "security":    2,
    "kernel":      3,
    "unknown":     4,
}

# Max values used for normalisation
_MAX_EVENT_ID  = 65535.0
_MAX_LEVEL     = 5.0
_MAX_SEVERITY  = 24.0
_MAX_PID       = 100000.0
_MAX_DESC_LEN  = 2000.0


def _clean_text(text: str) -> str:
    t = _UUID_RE.sub("UUID", text)
    t = _IP_RE.sub("IPADDR", t)
    t = _HEX_RE.sub("HEXVAL", t)
    t = _PATH_RE.sub("FILEPATH", t)
    t = _WIN_PATH.sub("WINPATH", t)
    t = _NUM_RE.sub("NUM", t)
    return t.lower()


class LogFeatureExtractor:
    """
    Stateful feature extractor.  One instance per agent/session — maintains
    rolling window state across successive calls to extract_one().
    """

    TEXT_DIM = 32

    def __init__(self):
        self._vectorizer = HashingVectorizer(
            n_features=self.TEXT_DIM,
            ngram_range=(1, 2),
            norm="l2",
            alternate_sign=False,
        )

        # Rolling window buffers
        self._window_50: deque[dict] = deque(maxlen=50)
        self._window_10: deque[dict] = deque(maxlen=10)

        # Track timing for event-rate calculation
        self._last_ts: float = time.time()

        # Feature name registry (built on first call)
        self._feature_names: list[str] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def extract_one(self, event: dict[str, Any]) -> np.ndarray:
        feat = np.concatenate([
            self._numeric_features(event),
            self._window_features(),
            self._text_features(event.get("Description", "")),
        ])
        # Update rolling windows AFTER extracting (so current event not included)
        self._window_50.append(event)
        self._window_10.append(event)

        if not self._feature_names:
            self._build_feature_names(len(feat))

        return feat.astype(np.float32)

    def extract_batch(self, events: list[dict]) -> np.ndarray:
        return np.vstack([self.extract_one(e) for e in events])

    @property
    def feature_names(self) -> list[str]:
        return self._feature_names

    # ── Feature groups ────────────────────────────────────────────────────────

    def _numeric_features(self, event: dict) -> np.ndarray:
        eid      = min(event.get("EventId", 0), _MAX_EVENT_ID) / _MAX_EVENT_ID
        level    = min(event.get("Level", 4), _MAX_LEVEL) / _MAX_LEVEL
        severity = min(event.get("SeverityNumber", 9), _MAX_SEVERITY) / _MAX_SEVERITY
        pid      = min(abs(event.get("ProcessId", 0)), _MAX_PID) / _MAX_PID

        channel_raw = event.get("Channel", "unknown").lower().split(".")[-1]
        channel_ord = _CHANNEL_ORD.get(channel_raw, len(_CHANNEL_ORD)) / (len(_CHANNEL_ORD) + 1)

        level_ord = (event.get("Level", 4) - 1) / (_MAX_LEVEL - 1)

        provider_hash = (abs(hash(event.get("Provider", ""))) % 10000) / 10000.0

        desc_len = min(event.get("DescriptionLen", 0), _MAX_DESC_LEN) / _MAX_DESC_LEN
        has_desc = float(bool(event.get("HasDescription", False)))

        # Circular time encoding
        ts_str = event.get("TimestampUtc", "")
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            dt = datetime.now(timezone.utc)

        hour   = dt.hour + dt.minute / 60.0
        dow    = dt.weekday()
        h_sin  = math.sin(2 * math.pi * hour / 24)
        h_cos  = math.cos(2 * math.pi * hour / 24)
        d_sin  = math.sin(2 * math.pi * dow / 7)
        d_cos  = math.cos(2 * math.pi * dow / 7)

        return np.array([
            eid, level, severity, pid,
            channel_ord, level_ord, provider_hash,
            desc_len, has_desc,
            h_sin, h_cos, d_sin, d_cos,
        ], dtype=np.float32)

    def _window_features(self) -> np.ndarray:
        now = time.time()
        elapsed = max(now - self._last_ts, 1e-6)
        self._last_ts = now

        def _stats(window: deque) -> tuple[float, float, float]:
            n = len(window)
            if n == 0:
                return 0.0, 0.0, 0.0
            count_norm  = min(n / window.maxlen, 1.0)
            error_rate  = sum(
                1 for e in window if e.get("Level", 4) <= 2
            ) / n
            event_rate  = min(n / (elapsed * 10), 1.0)   # events / 10s, capped
            return count_norm, error_rate, event_rate

        c10, er10, rate10 = _stats(self._window_10)
        c50, er50, rate50 = _stats(self._window_50)

        return np.array([c10, er10, rate10, c50, er50, rate50], dtype=np.float32)

    def _text_features(self, description: str) -> np.ndarray:
        if not description or not description.strip():
            return np.zeros(self.TEXT_DIM, dtype=np.float32)
        cleaned = _clean_text(description)
        vec = self._vectorizer.transform([cleaned])
        arr = vec.toarray()[0]
        # Scale to [0, 1] — HashingVectorizer with l2 norm is already bounded
        return arr.astype(np.float32)

    def _build_feature_names(self, total: int):
        numeric = [
            "event_id_norm", "level_norm", "severity_norm", "proc_id_norm",
            "channel_ord", "level_ord", "provider_hash",
            "desc_len_norm", "has_desc",
            "hour_sin", "hour_cos", "day_sin", "day_cos",
        ]
        window = [
            "w10_count", "w10_error_rate", "w10_event_rate",
            "w50_count", "w50_error_rate", "w50_event_rate",
        ]
        text = [f"text_{i}" for i in range(self.TEXT_DIM)]
        self._feature_names = numeric + window + text
        assert len(self._feature_names) == total, (
            f"Feature name count mismatch: {len(self._feature_names)} vs {total}"
        )
