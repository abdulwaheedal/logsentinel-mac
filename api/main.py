"""
api/main.py
──────────────────────────────────────────────────────────────────────────────
FastAPI backend — receives log batches from the agent, runs ML inference,
persists results to SQLite, and exposes query endpoints for the dashboard.
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from ml_pipeline.preprocessor    import LogFeatureExtractor
from ml_pipeline.anomaly_detector import AnomalyDetector

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH    = Path(os.environ.get("DB_PATH",    "data/anomalies.db"))
MODEL_DIR  = os.environ.get("MODEL_DIR",  "models")
CONTAMINATION = float(os.environ.get("CONTAMINATION", "0.05"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("api")

# ── Global ML state ───────────────────────────────────────────────────────────
_extractor: Optional[LogFeatureExtractor] = None
_detector:  Optional[AnomalyDetector]    = None
_input_dim: int                          = 51   # updated after first event

_loop_lock = asyncio.Lock()

# ── DB helpers ────────────────────────────────────────────────────────────────

def _init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ingested_at   TEXT    NOT NULL,
            timestamp_utc TEXT,
            channel       TEXT,
            provider      TEXT,
            level         INTEGER,
            level_name    TEXT,
            event_id      INTEGER,
            process_id    INTEGER,
            description   TEXT,
            is_anomaly    INTEGER NOT NULL DEFAULT 0,
            anomaly_score REAL    NOT NULL DEFAULT 0,
            if_score      REAL    NOT NULL DEFAULT 0,
            ae_score      REAL    NOT NULL DEFAULT 0,
            ensemble_mode TEXT,
            inference_ms  REAL
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_is_anomaly ON events(is_anomaly)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ingested_at ON events(ingested_at)")
    con.commit()
    con.close()


def _get_con() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _insert_events(rows: list[dict]):
    con = _get_con()
    con.executemany("""
        INSERT INTO events
            (ingested_at, timestamp_utc, channel, provider, level, level_name,
             event_id, process_id, description,
             is_anomaly, anomaly_score, if_score, ae_score, ensemble_mode, inference_ms)
        VALUES
            (:ingested_at,:timestamp_utc,:channel,:provider,:level,:level_name,
             :event_id,:process_id,:description,
             :is_anomaly,:anomaly_score,:if_score,:ae_score,:ensemble_mode,:inference_ms)
    """, rows)
    con.commit()
    con.close()


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _extractor, _detector
    _init_db()
    _extractor = LogFeatureExtractor()
    # Detector initialised lazily after first batch (need input_dim)
    log.info("ML pipeline ready. API listening.")
    yield
    log.info("Shutting down.")


app = FastAPI(title="LogSentinel Mac", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class RawEvent(BaseModel):
    EventId:        int   = 0
    RecordId:       int   = 0
    Channel:        str   = "unknown"
    Provider:       str   = ""
    Computer:       str   = ""
    Level:          int   = 4
    LevelName:      str   = "Information"
    SeverityNumber: int   = 9
    TimestampUtc:   str   = ""
    TimestampUnix:  int   = 0
    ProcessId:      int   = 0
    ThreadId:       int   = 0
    UserId:         str   = ""
    Description:    str   = ""
    TaskCategory:   int   = 0
    Keywords:       list  = []
    HasDescription: bool  = False
    DescriptionLen: int   = 0
    RawXml:         str   = ""

    class Config:
        extra = "allow"   # accept _mac_* extras silently


class IngestPayload(BaseModel):
    events: list[RawEvent]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/ingest", status_code=202)
async def ingest(payload: IngestPayload):
    global _detector, _input_dim

    if not payload.events:
        return {"accepted": 0, "anomalies": 0}

    rows      = []
    n_anomaly = 0
    now_str   = datetime.now(timezone.utc).isoformat()

    async with _loop_lock:
        for ev in payload.events:
            ev_dict = ev.model_dump()

            # Feature extraction
            feat = await asyncio.get_event_loop().run_in_executor(
                None, _extractor.extract_one, ev_dict
            )

            # Lazy detector init
            if _detector is None:
                _input_dim = len(feat)
                _detector  = AnomalyDetector(_input_dim, MODEL_DIR, CONTAMINATION)
                log.info("AnomalyDetector initialised (input_dim=%d)", _input_dim)

            # Inference
            result = await asyncio.get_event_loop().run_in_executor(
                None, _detector.predict, feat
            )

            if result.is_anomaly:
                n_anomaly += 1

            rows.append({
                "ingested_at":   now_str,
                "timestamp_utc": ev.TimestampUtc,
                "channel":       ev.Channel,
                "provider":      ev.Provider,
                "level":         ev.Level,
                "level_name":    ev.LevelName,
                "event_id":      ev.EventId,
                "process_id":    ev.ProcessId,
                "description":   ev.Description[:500] if ev.Description else "",
                "is_anomaly":    int(result.is_anomaly),
                "anomaly_score": result.anomaly_score,
                "if_score":      result.if_score,
                "ae_score":      result.ae_score,
                "ensemble_mode": result.ensemble_mode,
                "inference_ms":  result.inference_ms,
            })

    # DB write outside lock
    await asyncio.get_event_loop().run_in_executor(None, _insert_events, rows)

    return {"accepted": len(rows), "anomalies": n_anomaly}


@app.get("/anomalies")
def get_anomalies(
    anomaly_only: bool = Query(True),
    channel:      Optional[str]  = None,
    limit:        int  = Query(100, le=1000),
    offset:       int  = 0,
):
    con = _get_con()
    clauses = []
    params  = []

    if anomaly_only:
        clauses.append("is_anomaly = 1")
    if channel:
        clauses.append("channel = ?")
        params.append(channel)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows  = con.execute(
        f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/stats")
def get_stats():
    con = _get_con()
    row = con.execute("""
        SELECT
            COUNT(*)                           AS total_events,
            SUM(is_anomaly)                    AS total_anomalies,
            ROUND(AVG(anomaly_score), 4)       AS avg_score,
            ROUND(AVG(inference_ms), 2)        AS avg_inference_ms,
            MAX(ingested_at)                   AS last_event_at
        FROM events
    """).fetchone()
    total    = row["total_events"] or 0
    anomalies = row["total_anomalies"] or 0
    con.close()
    return {
        "total_events":    total,
        "total_anomalies": anomalies,
        "anomaly_rate":    round(anomalies / total, 4) if total else 0.0,
        "avg_score":       row["avg_score"]       or 0.0,
        "avg_inference_ms": row["avg_inference_ms"] or 0.0,
        "last_event_at":   row["last_event_at"],
    }


@app.get("/channels")
def get_channels():
    con  = _get_con()
    rows = con.execute(
        "SELECT DISTINCT channel FROM events ORDER BY channel"
    ).fetchall()
    con.close()
    return [r["channel"] for r in rows]


@app.get("/health")
def health():
    return {
        "status":         "ok",
        "if_trained":     _detector._if._trained if _detector else False,
        "ae_trained":     _detector._ae._trained if _detector else False,
        "db_path":        str(DB_PATH),
        "model_dir":      MODEL_DIR,
    }


@app.delete("/data")
def clear_data():
    con = _get_con()
    con.execute("DELETE FROM events")
    con.commit()
    con.close()
    return {"cleared": True}
