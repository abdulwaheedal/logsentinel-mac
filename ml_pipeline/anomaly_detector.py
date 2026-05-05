"""
ml_pipeline/anomaly_detector.py
──────────────────────────────────────────────────────────────────────────────
Two-tier anomaly detection:

    Tier 1  —  Isolation Forest  (always available, retrains every 500 events)
    Tier 2  —  Autoencoder       (optional PyTorch, trains on 1000 events)

Ensemble:
    • Before AE trained  →  IF score only,     threshold 0.65
    • Both trained       →  0.4×IF + 0.6×AE,  threshold 0.55
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

log = logging.getLogger(__name__)

# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class AnomalyResult:
    is_anomaly:    bool
    anomaly_score: float          # 0 (normal) → 1 (anomaly)
    if_score:      float
    ae_score:      float
    ensemble_mode: str            # "if_only" | "ensemble"
    threshold:     float
    inference_ms:  float
    extra:         dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Isolation Forest tier ─────────────────────────────────────────────────────

class IsolationForestDetector:
    RETRAIN_EVERY = 500

    def __init__(self, model_dir: str = "models", contamination: float = 0.05):
        self._model_dir    = Path(model_dir)
        self._contamination = contamination
        self._model_dir.mkdir(parents=True, exist_ok=True)

        self._scaler: Optional[StandardScaler]   = None
        self._forest: Optional[IsolationForest]  = None
        self._buffer: list[np.ndarray]           = []
        self._trained = False

        self._load_if_exists()

    # ── Public ─────────────────────────────────────────────────────────────

    def score(self, features: np.ndarray) -> float:
        """Return anomaly score in [0, 1].  Accumulates buffer for retraining."""
        self._buffer.append(features)

        if len(self._buffer) >= self.RETRAIN_EVERY:
            self._retrain()

        if not self._trained:
            return 0.0

        x = self._scaler.transform(features.reshape(1, -1))
        raw = self._forest.decision_function(x)[0]       # negative = anomaly
        # Map to [0, 1]: decision_function range ≈ [-0.5, 0.5]
        score = float(np.clip(0.5 - raw, 0, 1))
        return score

    # ── Internal ────────────────────────────────────────────────────────────

    def _retrain(self):
        log.info("Retraining Isolation Forest on %d events…", len(self._buffer))
        X = np.vstack(self._buffer)
        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X)

        self._forest = IsolationForest(
            n_estimators=200,
            contamination=self._contamination,
            random_state=42,
            n_jobs=-1,
        )
        self._forest.fit(X_scaled)
        self._trained = True
        self._buffer.clear()
        self._persist()
        log.info("Isolation Forest retrained ✓")

    def _persist(self):
        try:
            joblib.dump(self._forest, self._model_dir / "isolation_forest.pkl")
            joblib.dump(self._scaler,  self._model_dir / "if_scaler.pkl")
        except Exception as exc:
            log.warning("Could not save IF model: %s", exc)

    def _load_if_exists(self):
        f_path = self._model_dir / "isolation_forest.pkl"
        s_path = self._model_dir / "if_scaler.pkl"
        if f_path.exists() and s_path.exists():
            try:
                self._forest  = joblib.load(f_path)
                self._scaler  = joblib.load(s_path)
                self._trained = True
                log.info("Loaded pre-trained Isolation Forest from disk ✓")
            except Exception as exc:
                log.warning("Could not load IF model: %s", exc)


# ── Autoencoder tier (optional PyTorch) ───────────────────────────────────────

class AutoencoderDetector:
    TRAIN_AFTER = 1000
    THRESHOLD_PERCENTILE = 95

    def __init__(self, input_dim: int, model_dir: str = "models"):
        self._input_dim = input_dim
        self._model_dir = Path(model_dir)
        self._model_dir.mkdir(parents=True, exist_ok=True)
        self._model      = None
        self._threshold  = None
        self._scaler     = None
        self._buffer: list[np.ndarray] = []
        self._trained    = False
        self._torch_ok   = self._check_torch()

    def _check_torch(self) -> bool:
        try:
            import torch  # noqa: F401
            return True
        except ImportError:
            log.info("PyTorch not installed — autoencoder tier disabled.")
            return False

    def score(self, features: np.ndarray) -> float:
        if not self._torch_ok:
            return 0.0

        self._buffer.append(features)
        if len(self._buffer) >= self.TRAIN_AFTER and not self._trained:
            self._train()
        if not self._trained:
            return 0.0

        return float(np.clip(self._reconstruction_error(features) / (self._threshold + 1e-9), 0, 1))

    def _train(self):
        import torch
        import torch.nn as nn

        log.info("Training Autoencoder on %d events…", len(self._buffer))
        X = np.vstack(self._buffer)
        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X).astype(np.float32)

        d = self._input_dim
        model = nn.Sequential(
            nn.Linear(d, 64), nn.ReLU(),
            nn.Linear(64, 16), nn.ReLU(),
            nn.Linear(16, 64), nn.ReLU(),
            nn.Linear(64, d),
        )
        opt = torch.optim.Adam(model.parameters(), lr=1e-3)
        tensor = torch.tensor(X_scaled)

        model.train()
        for epoch in range(30):
            opt.zero_grad()
            loss = nn.functional.mse_loss(model(tensor), tensor)
            loss.backward()
            opt.step()
            if epoch % 10 == 0:
                log.debug("AE epoch %d loss=%.4f", epoch, loss.item())

        model.eval()
        with torch.no_grad():
            recon  = model(tensor).numpy()
        errors = np.mean((X_scaled - recon) ** 2, axis=1)
        self._threshold = float(np.percentile(errors, self.THRESHOLD_PERCENTILE))
        self._model     = model
        self._trained   = True
        self._buffer.clear()
        log.info("Autoencoder trained ✓  threshold=%.4f", self._threshold)

    def _reconstruction_error(self, features: np.ndarray) -> float:
        import torch
        x = self._scaler.transform(features.reshape(1, -1)).astype(np.float32)
        t = torch.tensor(x)
        self._model.eval()
        with torch.no_grad():
            recon = self._model(t).numpy()
        return float(np.mean((x - recon) ** 2))


# ── Ensemble: public interface ─────────────────────────────────────────────────

class AnomalyDetector:
    """
    Drop-in detector — call predict(feature_vector) per event.
    """

    _IF_ONLY_THRESHOLD  = 0.65
    _ENSEMBLE_THRESHOLD = 0.55
    _AE_WEIGHT          = 0.6
    _IF_WEIGHT          = 0.4

    def __init__(
        self,
        input_dim:     int,
        model_dir:     str   = "models",
        contamination: float = 0.05,
    ):
        self._if = IsolationForestDetector(model_dir, contamination)
        self._ae = AutoencoderDetector(input_dim, model_dir)

    def predict(self, features: np.ndarray) -> AnomalyResult:
        t0 = time.perf_counter()

        if_score = self._if.score(features)
        ae_score = self._ae.score(features)

        if self._ae._trained:
            combined = self._IF_WEIGHT * if_score + self._AE_WEIGHT * ae_score
            mode      = "ensemble"
            threshold = self._ENSEMBLE_THRESHOLD
        else:
            combined  = if_score
            mode      = "if_only"
            threshold = self._IF_ONLY_THRESHOLD

        combined     = float(np.clip(combined, 0, 1))
        is_anomaly   = combined >= threshold
        elapsed_ms   = (time.perf_counter() - t0) * 1000

        return AnomalyResult(
            is_anomaly    = is_anomaly,
            anomaly_score = combined,
            if_score      = if_score,
            ae_score      = ae_score,
            ensemble_mode = mode,
            threshold     = threshold,
            inference_ms  = round(elapsed_ms, 2),
        )
