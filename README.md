# LogSentinel — macOS Port

Real-time ML anomaly detection for **Apple Unified Logging System (ULS)** logs.
Ported from the Windows Event Log version by replacing the C# agent with a native Python collector.

---

## Architecture

```
macOS Machine
┌──────────────────────────────────────────────────────────┐
│  Apple Unified Logging System                            │
│  (kernel, system, app, security subsystems)              │
│                    │ log stream --style json              │
│           ┌────────▼───────────┐                         │
│           │  mac_log_agent.py  │  Python replacement      │
│           │  ─────────────     │  for C# LogCollector     │
│           │  • log stream sub  │                         │
│           │  • EventBuffer     │                         │
│           │  • normalise_event │                         │
│           │  • HTTP POST       │                         │
│           └────────┬───────────┘                         │
└────────────────────┼─────────────────────────────────────┘
                     │ Structured JSON (batches)
                     ▼
         FastAPI Backend (api/main.py)
         ─────────────────────────────
         LogFeatureExtractor  →  AnomalyDetector
                                 ├─ IsolationForest (Tier 1)
                                 └─ Autoencoder    (Tier 2)
                     │
                     ▼
           SQLite / Postgres
                     │
                     ▼
         dashboard/index.html  (static, open in browser)
```

---

## What Changed from Windows

| Component | Windows | macOS |
|-----------|---------|-------|
| Agent | `agent/LogCollectorAgent.cs` (C# .NET 8) | `agent/mac_log_agent.py` (Python) |
| Log source | Windows Event Log (`EvtSubscribe`) | Apple ULS (`log stream --style json`) |
| Service install | `scripts/install-service.ps1` (PowerShell) | `scripts/install-launchdaemon.sh` (bash) |
| Service type | Windows Service | macOS LaunchDaemon |
| ML pipeline | identical | identical |
| API | identical | identical |
| Dashboard | identical (bugs fixed) | bugs fixed |

---

## Quick Start

### 1 — Install dependencies
```bash
pip install -r requirements.txt
```

### 2 — Start the FastAPI backend
```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```
Verify: http://localhost:8000/health

### 3 — Start the macOS log agent
```bash
# Basic (info-level logs)
python agent/mac_log_agent.py

# Capture debug-level logs too
python agent/mac_log_agent.py --level debug

# Filter to a specific subsystem
python agent/mac_log_agent.py --predicate 'subsystem == "com.apple.kernel"'

# Custom backend URL
python agent/mac_log_agent.py --url http://myserver:8000
```
> Run with `sudo` for kernel and security-level subsystems.

### 4 — Open the dashboard
Open `dashboard/index.html` in your browser.  
Make sure the API URL in the top-right reads `http://localhost:8000`.

### 5 — Install as a permanent service (auto-start on boot)
```bash
sudo bash scripts/install-launchdaemon.sh
# With custom backend URL:
sudo bash scripts/install-launchdaemon.sh --url http://localhost:8000 --level info

# Uninstall:
sudo bash scripts/install-launchdaemon.sh --uninstall
```

---

## Dashboard Bug Fixes

The following bugs were fixed in `dashboard/index.html`:

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| Data not showing | API fetched `anomaly_only=true` by default — no events shown in live feed until anomalies occurred | Changed to `anomaly_only=false`, filter anomalies client-side |
| Infinite expand | `Chart.js` + `responsive:true` + `maintainAspectRatio:true` inside a flex container with no fixed height causes a resize feedback loop | Added explicit `height` to chart panels, set `maintainAspectRatio:false`, used `chart.update("none")` to skip animation |
| Feed unbounded growth | Feed items were only added, never pruned | DOM trimmed to `MAX_FEED_ITEMS=80` entries |

---

## macOS ULS Log Schema

Events from `log stream --style json` are normalised to the shared pipeline schema:

| ULS Field | Pipeline Field |
|-----------|---------------|
| `messageType` (Default/Info/Debug/Error/Fault) | `Level` (4/4/5/2/1) |
| `subsystem` | `Channel` |
| `processImagePath` (basename) | `Provider` |
| `processID` | `ProcessId` |
| `eventMessage` | `Description` |
| `timestamp` | `TimestampUtc` |
| `hash(subsystem:category)` | `EventId` |

---

## Run Tests
```bash
pytest tests/ -v
```

---

## Production Notes
- Replace SQLite with PostgreSQL for high-volume use (`DB_PATH` env var).
- The LaunchDaemon runs as root — required to read kernel and security subsystems.
- For non-root capture of app-level logs, convert to a `LaunchAgent` in `~/Library/LaunchAgents/`.
- Add an HTTPS reverse proxy (nginx/Caddy) in front of FastAPI for remote access.
