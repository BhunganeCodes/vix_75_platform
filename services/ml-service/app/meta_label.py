"""Triple-barrier meta-labeling + LightGBM meta-model.

Labeling (vectorized, lookahead-safe):

* Vertical barrier: 12 bars forward -> label 0.
* Horizontal barriers: +/- 2 * ATR from entry close -> label +1 (upper
  hit first), -1 (lower hit first).
* The scan walks the horizon offsets k = 1..12 and records FIRST contact
  per row using running masks; each step is a full-array vectorized op -
  there are no per-row Python loops anywhere.

Feature matrix: numeric columns from the ``features`` hypertable plus HMM
posteriors (S0/S1/S2) and cyclical time-of-day encodings.
"""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import numpy.typing as npt
from vix_core.artifacts import load_artifact, save_artifact
from vix_core.logging import get_logger

logger = get_logger(__name__)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

VERTICAL_BARS = 12
ATR_MULT = 2.0
ARTIFACT_PATH = "models/meta_label_lgbm.joblib"
CLASSES: tuple[int, ...] = (-1, 0, 1)


def apply_triple_barrier(
    close: FloatArray,
    atr_values: FloatArray,
    *,
    horizon: int = VERTICAL_BARS,
    atr_mult: float = ATR_MULT,
) -> IntArray:
    """Vectorized triple-barrier labels over a close/ATR series.

    Returns labels aligned to the ENTRY bar t:
        +1 upper barrier (close[t] + mult*ATR[t]) touched first,
        -1 lower barrier touched first,
         0 vertical barrier (t+horizon) reached first.
    Rows without a complete forward window are labeled 0 (vertical).
    """
    if close.shape != atr_values.shape:
        raise ValueError("close/atrs shape mismatch")
    n = close.size
    labels = np.zeros(n, dtype=np.int64)

    upper = close + atr_mult * atr_values
    lower = close - atr_mult * atr_values

    # First-hit bookkeeping; np.inf marks "not yet hit".
    up_step = np.full(n, np.inf)
    dn_step = np.full(n, np.inf)

    for k in range(1, horizon + 1):
        if k >= n:
            break  # no forward data left; remaining rows stay vertical
        future_close = close[k:]
        valid = slice(0, n - k)
        hit_up = future_close >= upper[valid]
        hit_dn = future_close <= lower[valid]

        new_up = hit_up & ~np.isfinite(up_step[valid])
        new_dn = hit_dn & ~np.isfinite(dn_step[valid])

        up_step[valid][new_up] = k
        dn_step[valid][new_dn] = k

    has_up = np.isfinite(up_step)
    has_dn = np.isfinite(dn_step)
    up_first = has_up & (~has_dn | (up_step <= dn_step))
    dn_first = has_dn & (~has_up | (dn_step < up_step))

    labels[up_first] = 1
    labels[dn_first] = -1
    return labels


def build_meta_features(
    feature_columns: list[str],
    feature_matrix: FloatArray,
    regime_probs: FloatArray | None = None,
    timestamps: npt.NDArray[np.datetime64] | None = None,
) -> tuple[list[str], FloatArray]:
    """Assemble the meta-model design matrix.

    Adds cyclical time-of-day encodings when timestamps are supplied and
    appends HMM posteriors when available. Column order defines inference
    layout and is persisted inside the artifact.
    """
    blocks: list[FloatArray] = [feature_matrix]
    names = list(feature_columns)

    if timestamps is not None and timestamps.size:
        hours = (timestamps.astype("datetime64[h]").astype(np.int64) % 24).astype(np.float64)
        tod_sin = np.sin(2.0 * np.pi * hours / 24.0).reshape(-1, 1)
        tod_cos = np.cos(2.0 * np.pi * hours / 24.0).reshape(-1, 1)
        blocks += [tod_sin, tod_cos]
        names += ["tod_sin", "tod_cos"]

    if regime_probs is not None and regime_probs.size:
        blocks.append(regime_probs)
        names += ["p_s0", "p_s1", "p_s2"]

    return names, np.hstack(blocks)


def train_meta_label(
    x: FloatArray,
    y: IntArray,
    *,
    feature_names: list[str] | None = None,
    artifact_path: str = ARTIFACT_PATH,
) -> dict:
    """Train LightGBM multiclass with 5-fold time-series CV; save artifact."""
    import lightgbm as lgb
    from sklearn.metrics import log_loss
    from sklearn.model_selection import TimeSeriesSplit

    if x.ndim != 2 or len(x) == 0:
        raise ValueError("X must be a non-empty 2-D array")
    classes = np.unique(y)
    if not set(classes.tolist()) <= set(CLASSES):
        raise ValueError(f"labels must be subset of {CLASSES}, got {classes}")

    params: dict[str, Any] = {
        "objective": "multiclass",
        "num_class": 3,
        "n_estimators": 300,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_child_samples": 40,
        "subsample": 0.9,
        "colsample_bytree": 0.9,
        "reg_lambda": 1.0,
        "random_state": 42,
        "verbosity": -1,
    }

    splitter = TimeSeriesSplit(n_splits=5)
    fold_losses: list[float] = []
    for fold, (train_idx, test_idx) in enumerate(splitter.split(x), start=1):
        model = lgb.LGBMClassifier(**params)
        model.fit(x[train_idx], y[train_idx])
        proba = model.predict_proba(x[test_idx])
        loss = float(log_loss(y[test_idx], proba, labels=list(CLASSES)))
        fold_losses.append(loss)
        logger.info("cv fold done", fold=fold, log_loss=round(loss, 4), rows=len(test_idx))

    final = lgb.LGBMClassifier(**params)
    final.fit(x, y)
    logger.info(
        "meta-label trained",
        rows=len(x),
        mean_cv_log_loss=round(float(np.mean(fold_losses)), 4),
    )

    bundle = {
        "model": final,
        "classes": list(CLASSES),
        "feature_names": feature_names or [],
        "cv_log_losses": fold_losses,
        "horizon": VERTICAL_BARS,
        "atr_mult": ATR_MULT,
    }
    save_artifact(bundle, artifact_path)
    return bundle


def load_meta_model(artifact_path: str = ARTIFACT_PATH) -> dict:
    return cast(dict, load_artifact(artifact_path))


def predict_proba(bundle: dict, x_row: FloatArray) -> dict[str, float]:
    """P(win-up) and P(down) for one feature row via the meta-model."""
    model = bundle["model"]
    classes = bundle.get("classes", list(CLASSES))
    row = x_row.reshape(1, -1)
    proba = model.predict_proba(row)[0]

    def class_p(target: int) -> float:
        idx = classes.index(target)
        return round(float(proba[idx]), 6)

    return {"p_up": class_p(1), "p_down": class_p(-1)}
