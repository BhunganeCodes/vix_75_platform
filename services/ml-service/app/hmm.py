"""3-state Gaussian HMM regime model (hmmlearn) with deterministic labels.

Features (per spec): ``log_return``, ``atr_norm = ATR/close``,
``realized_vol`` (rolling std of log-return, window 21).

hmmlearn assigns arbitrary state ids after fitting, so we canonicalize by
ordering states on their mean log-return: highest -> S1 (trend up),
lowest -> S2 (trend down), middle -> S0 (range). The mapping is stored in
the artifact bundle so inference is stable across reloads.
"""

from __future__ import annotations

import logging
from typing import cast

import numpy as np
import numpy.typing as npt
from vix_core.artifacts import load_artifact, save_artifact
from vix_core.logging import get_logger
from vix_core.schemas import RegimeState

logger = get_logger(__name__)

FloatArray = npt.NDArray[np.float64]

REGIME_FEATURES: tuple[str, ...] = ("log_return", "atr_norm", "realized_vol")
REALIZED_VOL_WINDOW = 21
ARTIFACT_PATH = "models/regime_hmm.joblib"

_SHORT_TO_REGIME: dict[str, str] = {
    "S0": str(RegimeState.S0_RANGE),
    "S1": str(RegimeState.S1_TREND_UP),
    "S2": str(RegimeState.S2_TREND_DOWN),
}


def build_regime_matrix(close: FloatArray, atr_values: FloatArray) -> FloatArray:
    """(log_return, atr_norm, realized_vol) matrix; warmup rows dropped.

    Row ``i`` describes the bar whose close is ``close[i + 1]``: that
    bar's log return, ATR ratio, and the std of the last 21 returns
    (causal window ending at the row itself).
    """
    if close.shape != atr_values.shape or close.size < REALIZED_VOL_WINDOW + 2:
        raise ValueError("need matching close/atr arrays of length >= 23")
    log_return = np.diff(np.log(close))
    atr_norm = (atr_values / close)[1:]

    windows = np.lib.stride_tricks.sliding_window_view(log_return, REALIZED_VOL_WINDOW)
    realized_vol = np.full(log_return.size, np.nan)
    realized_vol[REALIZED_VOL_WINDOW - 1 :] = windows.std(axis=1, ddof=1)

    complete = ~(np.isnan(atr_norm) | np.isnan(realized_vol))
    matrix = np.column_stack((log_return, atr_norm, realized_vol))[complete]
    return np.ascontiguousarray(matrix, dtype=np.float64)


def _canonical_order(model_means: npt.NDArray[np.float64]) -> dict[str, int]:
    """Map regime labels to state ids by mean log-return ranking."""
    mean_returns = model_means[:, 0]
    ranking = np.argsort(mean_returns)  # ascending
    return {
        "S2": int(ranking[0]),  # most negative drift -> down-trend state
        "S0": int(ranking[1]),  # middle -> range
        "S1": int(ranking[2]),  # most positive drift -> up-trend state
    }


def train_hmm(features: FloatArray, *, artifact_path: str = ARTIFACT_PATH) -> dict:
    """Fit GaussianHMM(k=3), canonicalize states, persist verified artifact."""
    from hmmlearn.hmm import GaussianHMM

    if features.ndim != 2 or features.shape[1] != len(REGIME_FEATURES):
        raise ValueError(f"features must be (n, {len(REGIME_FEATURES)})")
    if len(features) < 100:
        raise ValueError("HMM training needs >= 100 complete feature rows")

    model = GaussianHMM(
        n_components=3,
        covariance_type="diag",
        n_iter=500,
        tol=1e-4,
        min_covar=1e-6,
        random_state=42,
        verbose=False,
        # Fit emissions only; start/transition matrices stay at uniform
        # priors. Prevents the degenerate "no transition ever observed"
        # collapse on short/segmented histories (NaN transmat rows).
        init_params="c",
    )
    model.startprob_ = np.full(3, 1.0 / 3.0)
    model.transmat_ = np.full((3, 3), 1.0 / 3.0)

    logging.getLogger("hmmlearn.base").setLevel(logging.ERROR)  # EM chatter
    model.fit(features)
    order = _canonical_order(model.means_)
    scores = model.score(features)
    logger.info(
        "hmm trained",
        rows=len(features),
        log_likelihood=round(float(scores), 2),
        order=order,
    )
    bundle = {
        "model": model,
        "order": order,
        "features": list(REGIME_FEATURES),
        "path": artifact_path,
    }
    save_artifact(bundle, artifact_path)
    return bundle


def load_regime_model(artifact_path: str = ARTIFACT_PATH) -> dict:
    """Load the regime bundle; SHA256 verification is mandatory."""
    return cast(dict, load_artifact(artifact_path))


def predict_regime(
    bundle: dict, x_latest: FloatArray
) -> tuple[str, int, tuple[float, float, float]]:
    """Return (regime_label, raw_state_id, probs) for one feature row.

    ``probs`` is ordered (S0_range, S1_trend_up, S2_trend_down).
    """
    model = bundle["model"]
    order: dict[str, int] = bundle["order"]
    if x_latest.ndim == 1:
        x_latest = x_latest.reshape(1, -1)
    posterior = model.predict_proba(x_latest)[0]

    label_by_state = {state_id: label for label, state_id in order.items()}
    best_state = int(np.argmax(posterior))
    short_label = label_by_state[best_state]
    probs_s0s1s2 = (
        float(posterior[order["S0"]]),
        float(posterior[order["S1"]]),
        float(posterior[order["S2"]]),
    )
    regime_label = _SHORT_TO_REGIME.get(short_label, short_label)
    return regime_label, best_state, probs_s0s1s2
