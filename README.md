<div align="center">

<img width="1439" alt="LogSentinel Hero" src="https://github.com/user-attachments/assets/6a5d7ac6-5df9-41eb-8e0c-15ea81bec6ba" />

# LogSentinel — macOS Edition

**Real-time ML anomaly detection for Apple's Unified Logging System.**  
No Docker. No cloud. Runs entirely on your Mac.

[![Website](https://img.shields.io/badge/Website-sentinelog.netlify.app-7c6dfa?style=flat-square)](https://sentinelog.netlify.app)
[![Download](https://img.shields.io/badge/Download-v1.1.1-06d6a0?style=flat-square)](https://github.com/user-attachments/files/27407529/logsentinel-mac-v1.1.1.zip)
[![License](https://img.shields.io/badge/License-MIT-white?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-macOS%2012+-lightgrey?style=flat-square&logo=apple)](https://github.com/abdulwaheedal/logsentinel-mac/releases)
[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)

[Installation](#installation--usage) · [How It Works](#how-it-works) · [Tech Stack](#tech-stack) · [Training Notebook](https://colab.research.google.com/github/abdulwaheedal/AI_Anamoly_Detection_using_logs/blob/main/Anomaly_Detection_System_HDFS.ipynb)

</div>

---

## Overview

LogSentinel taps directly into macOS's **Unified Logging System (ULS)** — the same event stream the OS uses internally — and runs every log event through a two-tier machine learning ensemble to surface anomalies in real time. A lightweight local dashboard polls the API every 3 seconds and fires toast alerts on high-confidence detections.

Everything — feature extraction, model training, inference, storage, and the dashboard — runs locally on your Mac. **No data leaves your machine.**

<br>

<div align="center">

### Performance — benchmarked on HDFS (11.1 M log lines)

| F1 Score | Recall | Accuracy | Precision |
|:--------:|:------:|:--------:|:---------:|
| **71.6%** | **81.1%** | **98.1%** | **64%** |

</div>
<img width="1440" height="900" alt="Screenshot 2026-05-06 at 1 08 21 AM" src="https://github.com/user-attachments/assets/4aa7958a-a497-47e8-a857-ba75661f6cd3" />

---

## How It Works

```
Apple ULS  →  Python Agent  →  Feature Engineering  →  ML Ensemble  →  Dashboard
```

<details>
<summary><b>1 — Apple ULS Collection</b></summary>
<br>

The Python agent spawns `log stream --style json --level info` and subscribes to the macOS Unified Log stream in real time — every kernel, application, and system event, sourced directly from the OS.

</details>

<details>
<summary><b>2 — Agent & Event Buffer</b> &nbsp;<code>agent/mac_log_agent.py</code></summary>
<br>

Each raw ULS JSON event is normalised into a standard `StructuredLogEvent` schema. An `EventBuffer` micro-batches events — **50 events or 2 seconds**, whichever comes first — then HTTP POSTs to the FastAPI backend with automatic retry (3 attempts, 1 s backoff). Dropped events are counted and logged on shutdown.

Key fields extracted per event:

| Field | Description |
|---|---|
| `EventId` | Stable pseudo-ID from `subsystem:category` hash |
| `Channel`, `Provider`, `Level`, `SeverityNumber` | Standard log metadata |
| `TimestampUtc` | Parsed from macOS format `YYYY-MM-DD HH:MM:SS.ffffff±HH` |
| `ProcessId`, `ThreadId`, `Description` | Process context |
| `_mac_subsystem`, `_mac_category`, `_mac_process` | macOS-specific extras, passed through transparently |

</details>

<details>
<summary><b>3 — Feature Engineering</b> &nbsp;<code>ml_pipeline/preprocessor.py</code></summary>
<br>

Each event becomes a **51-dimensional float32 feature vector** across three groups:

| Group | Dims | What it captures |
|---|:---:|---|
| Numeric | 13 | `event_id`, `level`, `severity`, `pid`, channel ordinal, provider hash, description length, presence flag, circular `sin/cos` encoding of hour and weekday |
| Rolling window | 6 | Count, error rate, and event rate over the last 10 and 50 events |
| Text | 32 | `HashingVectorizer` (unigrams + bigrams, L2-normalised) on cleaned description — IPs, hex values, paths, UUIDs, and raw numbers are all normalised before hashing |

</details>

<details>
<summary><b>4 — ML Ensemble</b> &nbsp;<code>ml_pipeline/anomaly_detector.py</code></summary>
<br>

**Tier 1 — Isolation Forest** *(always active)*
- Retrains automatically every **500 events** to adapt to baseline drift
- `StandardScaler` → 200 estimators, `contamination = 0.05`
- Model persisted to `models/isolation_forest.pkl` and reloaded on restart
- Raw `decision_function` score mapped to `[0, 1]`

**Tier 2 — Autoencoder** *(optional — requires PyTorch)*
- Architecture: `d → 64 → 16 → 64 → d`
- Trains after **1000 events**; threshold = 95th percentile of training reconstruction errors
- 30 epochs, Adam optimiser, MSE loss

**Ensemble scoring:**

```
Before AE trained  →  score = IF score            threshold 0.65
After AE trained   →  score = 0.4×IF + 0.6×AE    threshold 0.55
```

</details>

<details>
<summary><b>5 — FastAPI Backend & SQLite</b> &nbsp;<code>api/main.py</code></summary>
<br>

Receives batches at `POST /ingest`, runs the full inference pipeline, and writes to a local SQLite database (`data/anomalies.db`). DB writes happen outside the async inference lock to avoid head-of-line blocking.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/ingest` | Receive event batch, run inference, persist results |
| `GET` | `/anomalies` | Query events — filter by `anomaly_only`, `channel`, `limit`, `offset` |
| `GET` | `/stats` | Total events, anomaly count, anomaly rate, avg score, avg latency |
| `GET` | `/channels` | All distinct channels seen |
| `GET` | `/health` | Liveness + IF/AE training status |
| `DELETE` | `/data` | Clear all stored events |

</details>

<details>
<summary><b>6 — Live Dashboard</b> &nbsp;<code>dashboard/index.html</code></summary>
<br>

A static HTML page that polls `/stats`, `/anomalies`, and `/channels` every **3 seconds**. Displays a live anomaly score trend (Chart.js), severity distribution, a filterable event feed, and browser toast alerts on high-confidence anomalies.

<img width="1063" alt="Dashboard Screenshot" src="https://github.com/user-attachments/assets/757c0a58-12bb-4ade-b128-7805128394c8" />

</details>

---

## Project Structure

```
logsentinel-mac-release/
│
├── agent/
│   └── mac_log_agent.py          # ULS collector, normaliser, EventBuffer
│
├── api/
│   ├── __init__.py
│   └── main.py                   # FastAPI backend, SQLite, all endpoints
│
├── ml_pipeline/
│   ├── __init__.py
│   ├── preprocessor.py           # LogFeatureExtractor — 51-dim feature vectors
│   └── anomaly_detector.py       # IsolationForest + Autoencoder ensemble
│
├── dashboard/
│   └── index.html                # Static dashboard, polls API every 3 s
│
├── scripts/
│   └── install-launchdaemon.sh   # Optional: run as a persistent launchd daemon
│
├── models/                       # Auto-created — persisted IF model lives here
├── data/                         # Auto-created — SQLite DB lives here
│
├── requirements.txt
├── SetupSentinel.command         # ① Double-click to set up venv + install deps
├── StartSentinel.command         # ② Double-click to start everything
└── StopSentinel.command          # ③ Double-click to stop everything
```

---

## Requirements

| Requirement | Version |
|---|---|
| macOS | 12 Monterey or later |
| Python | 3.9 or later |
| pip | Bundled with Python |
| Browser | Any modern browser |
| PyTorch *(optional)* | Any — enables Autoencoder tier |

---

## Installation & Usage

### Quick Start *(recommended)*

> **Note:** macOS may show a security prompt the first time you run a `.command` file. Right-click → **Open** to bypass Gatekeeper on first launch.

**Step 1** — [Download `logsentinel-mac-v1.1.1.zip`](https://github.com/user-attachments/files/27407529/logsentinel-mac-v1.1.1.zip) and unzip it.

**Step 2** — Double-click **`SetupSentinel.command`**  
Creates a Python virtual environment and installs all dependencies. Run this once.

**Step 3** — Double-click **`StartSentinel.command`**  
Starts the FastAPI backend, launches the log agent, and opens the dashboard in your browser.

**Step 4** — Double-click **`StopSentinel.command`** when done  
Gracefully shuts down all processes and closes the terminal windows.

<br>

### Manual Start

```bash
# 1. Set up the environment (once)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Start the backend
uvicorn api.main:app --host 0.0.0.0 --port 8000

# 3. In a separate terminal — start the agent
source venv/bin/activate
python agent/mac_log_agent.py

# 4. Open the dashboard
open dashboard/index.html
```

<br>

### Agent CLI Options

```
python agent/mac_log_agent.py [OPTIONS]

  --url TEXT        Backend URL               (default: http://localhost:8000)
  --level TEXT      Log level: default|info|debug  (default: info)
  --predicate TEXT  NSPredicate filter        e.g. 'subsystem == "com.apple.kernel"'
```

<br>

### Enable the Autoencoder *(optional)*

PyTorch is not a required dependency. To activate Tier 2:

```bash
source venv/bin/activate
pip install torch
```

Restart the backend — the autoencoder begins training automatically after 1000 events and joins the ensemble from that point on.

<br>

### Run as a launchd Daemon *(optional)*

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

## Tech Stack

| Component | Technology |
|---|---|
| Log source | Apple Unified Logging System (`log stream`) |
| Agent | Python 3 — `subprocess`, `threading`, `requests` |
| Backend | FastAPI + Uvicorn |
| Feature extraction | NumPy · scikit-learn `HashingVectorizer` |
| Tier 1 detection | scikit-learn `IsolationForest` |
| Tier 2 detection | PyTorch Autoencoder *(optional)* |
| Model persistence | joblib |
| Storage | SQLite |
| Dashboard | Vanilla HTML/JS + Chart.js |

---

## Limitations

- The agent must run on the same machine as the log source — remote collection is not currently supported.
- `sudo` may be required to capture kernel and security subsystem events: `sudo python agent/mac_log_agent.py`
- The autoencoder operates in warmup for its first 1000 events; the system runs in IF-only mode until then.
- The Isolation Forest re-baselines every 500 events — sustained anomalies that persist long enough will eventually be absorbed into the baseline. This is an intentional trade-off of unsupervised drift adaptation.

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

<div align="center">

Built end-to-end by **Faaiz** — ML pipeline, feature engineering, FastAPI backend, and macOS port.

<a href="https://github.com/abdulwaheedal" target="_blank">
  <img src="https://img.shields.io/badge/-Github-333333?style=for-the-badge&logo=github&logoColor=white" alt="Github" />
</a>
<a href="https://x.com/faaa1z" target="_blank">
  <img src="https://img.shields.io/badge/-Twitter-1DA1F2?style=for-the-badge&logo=x&logoColor=white" alt="Twitter" />
</a>
<a href="https://instagram.com/abdulwaheedal" target="_blank">
  <img src="https://img.shields.io/badge/-Instagram-ee2a7b?style=for-the-badge&logo=instagram&logoColor=white" alt="Instagram" />
</a>


</div>
