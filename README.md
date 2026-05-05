# [LogSentinel — macOS Edition](https://sentinelog.netlify.app) 

> Real-time ML anomaly detection for Apple's Unified Logging System.  
> No Docker. No cloud. Runs entirely on your Mac.

[**Website**](https://sentinelog.netlify.app)  <img width="1439" height="899" alt="Screenshot 2026-05-06 at 1 16 24 AM" src="https://github.com/user-attachments/assets/6a5d7ac6-5df9-41eb-8e0c-15ea81bec6ba" />

[**Download**](https://github.com/user-attachments/files/27407529/logsentinel-mac-v1.1.1.zip)  
[**Installation Guide**](https://github.com/abdulwaheedal/logsentinel-mac/edit/main/README.md#installation--usage)
[**Training Notebook**](https://colab.research.google.com/github/abdulwaheedal/AI_Anamoly_Detection_using_logs/blob/main/Anomaly_Detection_System_HDFS.ipynb)

---

<img width="1440" height="900" alt="Screenshot 2026-05-06 at 1 08 21 AM" src="https://github.com/user-attachments/assets/4aa7958a-a497-47e8-a857-ba75661f6cd3" />


---

## Overview

LogSentinel taps directly into macOS's **Unified Logging System (ULS)** — the same event stream the OS uses internally — and runs every log event through a two-tier machine learning ensemble to surface anomalies in real time. A lightweight local dashboard polls the API every 3 seconds and fires toast alerts on high-confidence detections.

Everything — feature extraction, model training, inference, storage, and the dashboard — runs locally on your Mac. No data leaves your machine.

### Performance (benchmarked on HDFS — 11.1 M log lines)

| Metric    | Score  |
|-----------|--------|
| F1 Score  | 71.6%  |
| Recall    | 81.1%  |
| Accuracy  | 98.1%  |
| Precision | 64%    |

---

## How It Works

LogSentinel has five stages, all running locally.

```
Apple ULS  →  Python Agent  →  Feature Engineering  →  ML Ensemble  →  Dashboard
```

### 1 — Apple ULS Collection

The Python agent spawns `log stream --style json --level info` and subscribes to the macOS Unified Log stream in real time — every kernel, application, and system event.

### 2 — Agent & Event Buffer (`agent/mac_log_agent.py`)

Each raw ULS JSON event is normalised into a standard `StructuredLogEvent` schema — compatible with both the macOS Python agent and any future Windows C# counterpart. An `EventBuffer` micro-batches events (50 events or 2 seconds, whichever comes first) and HTTP POSTs them to the FastAPI backend with automatic retry (3 attempts, 1-second backoff). Dropped events are counted and logged on shutdown.

**Key normalisation fields extracted:**
- `EventId` — stable pseudo-ID derived from `subsystem:category` hash
- `Channel`, `Provider`, `Level`, `SeverityNumber`
- `TimestampUtc` — parsed from macOS format (`YYYY-MM-DD HH:MM:SS.ffffff±HH`)
- `ProcessId`, `ThreadId`, `Description`
- macOS-specific extras (`_mac_subsystem`, `_mac_category`, `_mac_process`) passed through transparently

### 3 — Feature Engineering (`ml_pipeline/preprocessor.py`)

Each event is transformed into a **51-dimensional float32 feature vector** composed of three groups:

| Group | Dimensions | What it captures |
|---|---|---|
| Numeric | 13 | `event_id`, `level`, `severity`, `pid`, channel ordinal, provider hash, description length, presence flag, circular `sin/cos` encoding of hour and weekday |
| Rolling window | 6 | Count, error rate, and event rate over the last 10 and 50 events |
| Text (TF-IDF-style) | 32 | `HashingVectorizer` (unigrams + bigrams, L2-normalised) on cleaned description — IPs, hex values, paths, UUIDs, and numbers are all normalised before hashing |

### 4 — ML Ensemble (`ml_pipeline/anomaly_detector.py`)

Two-tier detection:

**Tier 1 — Isolation Forest** (always active)  
- Retrains automatically every **500 events** to adapt to baseline drift  
- Uses `StandardScaler` before fitting; 200 estimators, `contamination=0.05`  
- Model persisted to `models/isolation_forest.pkl` and reloaded on restart  
- Score mapped from `decision_function` output to `[0, 1]`

**Tier 2 — Autoencoder** (optional, requires PyTorch)  
- A 4-layer symmetric autoencoder: `d → 64 → 16 → 64 → d`  
- Trains after **1000 events** have accumulated; threshold set at the 95th percentile of reconstruction errors on training data  
- 30 epochs, Adam optimiser, MSE loss

**Ensemble scoring:**

```
Before AE trained:   score = IF score            threshold = 0.65
After AE trained:    score = 0.4×IF + 0.6×AE    threshold = 0.55
```

### 5 — FastAPI Backend & SQLite (`api/main.py`)

The backend receives event batches at `POST /ingest`, runs the full feature extraction and inference pipeline, and writes results to a local SQLite database (`data/anomalies.db`). All DB writes happen outside the async inference lock to avoid head-of-line blocking.

**Endpoints:**

| Method | Path | Description |
|---|---|---|
| `POST` | `/ingest` | Receive a batch of log events, run inference, store results |
| `GET` | `/anomalies` | Query stored events (filter by `anomaly_only`, `channel`, `limit`, `offset`) |
| `GET` | `/stats` | Aggregate stats: total events, anomaly count, anomaly rate, avg score, avg inference latency |
| `GET` | `/channels` | List of all distinct channels seen |
| `GET` | `/health` | API liveness + IF/AE training status |
| `DELETE` | `/data` | Clear all stored events |

### 6 — Dashboard (`dashboard/index.html`)

A static HTML page that polls `/stats`, `/anomalies`, and `/channels` every **3 seconds**. Displays a live anomaly score trend chart (Chart.js), severity distribution, a filterable live event feed, and browser toast alerts for events with high anomaly scores.

<img width="1063" height="636" alt="Screenshot 2026-05-06 at 1 14 43 AM" src="https://github.com/user-attachments/assets/757c0a58-12bb-4ade-b128-7805128394c8" />
---

## Project Structure

```
logsentinel-mac-release/
├── agent/
│   └── mac_log_agent.py          # ULS collector, event normaliser, EventBuffer
├── api/
│   ├── __init__.py
│   └── main.py                   # FastAPI backend, SQLite persistence, endpoints
├── ml_pipeline/
│   ├── __init__.py
│   ├── preprocessor.py           # LogFeatureExtractor (51-dim feature vectors)
│   └── anomaly_detector.py       # IsolationForest + Autoencoder ensemble
├── dashboard/
│   └── index.html                # Static dashboard, polls API every 3 seconds
├── scripts/
│   └── install-launchdaemon.sh   # Optional: run LogSentinel as a launchd daemon
├── models/                       # Auto-created; persisted IF model lives here
├── data/                         # Auto-created; SQLite DB lives here
├── requirements.txt
├── SetupSentinel.command         # Double-click: creates venv, installs deps
├── StartSentinel.command         # Double-click: starts API + agent + dashboard
└── StopSentinel.command          # Double-click: gracefully shuts everything down
```

---

## Requirements

- macOS 12 Monterey or later (macOS 10.12+ minimum for `log stream`)
- Python 3.9 or later
- `pip` (bundled with Python)
- Any modern browser (for the dashboard)
- *(Optional)* PyTorch — enables the Autoencoder (Tier 2). Install separately: `pip install torch`

---

## Installation & Usage

### Quick Start (recommended)

1. **Download** `logsentinel-mac-v1.1.1.zip` from the [releases page](https://github.com/abdulwaheedal/logsentinel-mac/releases) or the link at the top of this file.
2. **Unzip** the archive and open the extracted folder.
3. **Double-click `SetupSentinel.command`** — creates a Python virtual environment and installs all dependencies automatically. You only need to do this once.
4. **Double-click `StartSentinel.command`** — starts the FastAPI backend, launches the log agent, and opens the dashboard in your browser.
5. **Double-click `StopSentinel.command`** when done — gracefully shuts down all processes and closes the terminal windows.

> **Note:** macOS may show a security prompt the first time you run a `.command` file. Right-click → Open to bypass Gatekeeper on first launch.

### Manual Start

If you prefer the terminal:

```bash
# From the project root

# 1. Set up the environment (once)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Start the backend
uvicorn api.main:app --host 0.0.0.0 --port 8000

# 3. In a separate terminal, start the agent
source venv/bin/activate
python agent/mac_log_agent.py

# 4. Open the dashboard
open dashboard/index.html
```

### Agent CLI Options

```bash
python agent/mac_log_agent.py [OPTIONS]

Options:
  --url TEXT        FastAPI backend URL (default: http://localhost:8000)
  --level TEXT      Minimum log level: default | info | debug (default: info)
  --predicate TEXT  Optional NSPredicate filter, e.g. 'subsystem == "com.apple.kernel"'
```

### Optional: Enable the Autoencoder

The Autoencoder tier is disabled by default (no PyTorch dependency required). To enable it:

```bash
source venv/bin/activate
pip install torch
```

Restart the backend — the AE will begin training automatically after 1000 events have been ingested and will join the ensemble from that point on.

### Optional: Run as a launchd Daemon

For persistent background monitoring that survives reboots:

```bash
bash scripts/install-launchdaemon.sh
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PIPELINE_URL` | `http://localhost:8000` | Backend URL used by the agent |
| `DB_PATH` | `data/anomalies.db` | SQLite database path |
| `MODEL_DIR` | `models` | Directory for persisted model files |
| `CONTAMINATION` | `0.05` | Isolation Forest contamination parameter |

---

## Dependencies

```
fastapi>=0.111.0
uvicorn[standard]>=0.29.0
pydantic>=2.0
requests>=2.31.0
scikit-learn>=1.4.0
numpy>=1.26.0
joblib>=1.3.0

# Optional
torch   # pip install torch — enables Autoencoder tier
```

The dashboard uses **Chart.js** (CDN, no local install required).

---

## Tech Stack

| Component | Technology |
|---|---|
| Log source | Apple Unified Logging System (`log stream`) |
| Agent | Python 3 (`subprocess`, `threading`, `requests`) |
| Backend API | FastAPI + Uvicorn |
| Feature extraction | NumPy, scikit-learn (`HashingVectorizer`) |
| Tier 1 detection | scikit-learn `IsolationForest` |
| Tier 2 detection | PyTorch Autoencoder (optional) |
| Model persistence | joblib |
| Storage | SQLite |
| Dashboard | Vanilla HTML/JS + Chart.js |

---

## Limitations & Known Issues

- The agent must be running on the same machine as the log source — remote collection is not currently supported.
- `sudo` may be required to capture kernel and security subsystem events (`sudo python agent/mac_log_agent.py`).
- The Autoencoder requires 1000 events before it begins contributing to the ensemble score; the system operates in IF-only mode until then.
- The Isolation Forest baseline adapts every 500 events — sustained anomalies that persist long enough will eventually be absorbed into the baseline. This is a known trade-off of unsupervised drift adaptation.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Author

Built by **Faaiz** — ML pipeline, feature engineering, FastAPI backend, and macOS port end-to-end.  
GitHub: [@abdulwaheedal](https://github.com/abdulwaheedal)
