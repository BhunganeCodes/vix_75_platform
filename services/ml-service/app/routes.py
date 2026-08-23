"""ml-service routes: training triggers and inference endpoint.

POST /train  - spawns background HMM + meta-label jobs (audit-logged).
GET /predict - latest features -> regime + P(up)/P(down) from cached or
               freshly loaded models. Models load ONLY after SHA256
               verification (vix_core.artifacts).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, cast

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from vix_core.config import Settings
from vix_core.logging import get_logger

from .hmm import (
    ARTIFACT_PATH as HMM_PATH,
)
from .hmm import (
    build_regime_matrix,
    predict_regime,
    train_hmm,
)
from .meta_label import (
    ARTIFACT_PATH as META_PATH,
)
from .meta_label import (
    apply_triple_barrier,
    build_meta_features,
    predict_proba,
    train_meta_label,
)

logger = get_logger(__name__)
router = APIRouter()

# Only ONE training pipeline may run at a time - concurrent jobs race on
# the shared artifact files (joblib write + sidecar swap).
import threading  # noqa: E402

_TRAIN_LOCK = threading.Lock()


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


@router.get("/health")
async def health() -> dict[str, object]:
    return {"service": "ml-service", "status": "ok"}


@router.post("/train", status_code=202)
async def train(
    background: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    timeframe: str = "M15",
) -> dict[str, object]:
    """Queue full retraining of the regime + meta-label models."""
    if timeframe not in {"M5", "M15", "H1"}:
        raise HTTPException(status_code=422, detail=f"unsupported timeframe {timeframe}")

    async def _job() -> None:
        if not _TRAIN_LOCK.acquire(blocking=False):
            logger.warning("training already in progress; request skipped")
            return
        try:
            await asyncio.to_thread(_train_sync, settings, timeframe)
        except Exception:
            logger.exception("training job failed")
        finally:
            _TRAIN_LOCK.release()

    correlation_id = uuid.uuid4().hex
    background.add_task(_job)
    logger.info("training queued", timeframe=timeframe, correlation_id=correlation_id)
    return {
        "queued": True,
        "timeframe": timeframe,
        "artifacts": [HMM_PATH, META_PATH],
        "correlation_id": correlation_id,
    }


def _train_sync(settings: Settings, timeframe: str) -> dict[str, float]:
    """Blocking training pipeline executed in a worker thread."""
    from .db import fetch_close_atr, fetch_feature_matrix
    from .hmm import ARTIFACT_PATH as HMM_ART
    from .hmm import load_regime_model
    from .meta_label import VERTICAL_BARS

    dsn = settings.database_url
    ts_matrix, columns, matrix = fetch_feature_matrix(dsn, settings.symbol, timeframe)
    _, close, atr_values = fetch_close_atr(dsn, settings.symbol, timeframe)
    logger.info(
        "training data loaded",
        timeframe=timeframe,
        feature_rows=len(matrix),
        price_rows=len(close),
    )
    if len(matrix) < 500:
        raise RuntimeError(
            f"insufficient training history: {len(matrix)} rows (need >= 500);"
            " run a backfill first"
        )

    # ---- Regime model -------------------------------------------------
    hmm_x = build_regime_matrix(close, atr_values)
    train_hmm(hmm_x, artifact_path=HMM_ART)
    bundle = load_regime_model(HMM_ART)
    probs = bundle["model"].predict_proba(hmm_x[-len(matrix) :])
    n = min(len(matrix), len(probs))

    # ---- Meta labels ---------------------------------------------------
    labels = apply_triple_barrier(close, atr_values)
    # Align label series to the feature matrix tail; drop incomplete horizon.
    y_all = labels[-len(matrix) :]
    usable = min(n, len(y_all) - VERTICAL_BARS)
    x_names, x = build_meta_features(
        columns,
        matrix[-usable:],
        regime_probs=probs[-usable:],
        timestamps=ts_matrix[-usable:],
    )
    y = y_all[-usable:]

    meta_bundle = train_meta_label(
        np.ascontiguousarray(x), y.astype(np.int64), feature_names=x_names
    )
    losses = meta_bundle["cv_log_losses"]
    return {"mean_cv_log_loss": float(np.mean(losses))}


@router.get("/predict")
async def predict(
    settings: Annotated[Settings, Depends(get_settings)],
    timeframe: str = "M15",
) -> dict[str, object]:
    """Latest regime classification + meta-label probabilities."""
    try:
        regime_bundle = __load(HMM_PATH)
        meta_bundle = __load(META_PATH)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="models unavailable; POST /train first"
        ) from exc

    from .db import fetch_close_atr, fetch_feature_matrix

    ts_matrix, columns, matrix = await asyncio.to_thread(
        fetch_feature_matrix, settings.database_url, settings.symbol, timeframe, 64
    )
    if len(matrix) == 0:
        raise HTTPException(status_code=503, detail="no features stored yet")
    _, close, atr_values = await asyncio.to_thread(
        fetch_close_atr, settings.database_url, settings.symbol, timeframe
    )

    regime_label, state_id, probs = await asyncio.to_thread(
        _predict_latest, regime_bundle, close, atr_values
    )
    posterior_model = regime_bundle["model"]
    posteriors_full = np.asarray(
        posterior_model.predict_proba(__regime_matrix(close, atr_values))  # type: ignore[attr-defined]
    )
    names, x = build_meta_features(
        columns,
        matrix,
        regime_probs=posteriors_full[-len(matrix) :],
        timestamps=ts_matrix,
    )
    del names
    meta = predict_proba(meta_bundle, x[-1])

    return {
        "symbol": settings.symbol,
        "timeframe": timeframe,
        "ts": str(ts_matrix[-1]),
        "regime": regime_label,
        "state_id": state_id,
        "probabilities": {"S0": probs[0], "S1": probs[1], "S2": probs[2]},
        **meta,
    }


def __load(path: str) -> dict[str, object]:
    from .hmm import load_regime_model
    from .meta_label import load_meta_model

    loader = load_regime_model if path.endswith("regime_hmm.joblib") else load_meta_model
    loaded = loader(path)
    return cast("dict[str, object]", loaded)


def __regime_matrix(close: np.ndarray, atr_values: np.ndarray) -> np.ndarray:
    return build_regime_matrix(close, atr_values)


def _predict_latest(
    regime_bundle: dict[str, object], close: np.ndarray, atr_values: np.ndarray
) -> tuple[str, int, tuple[float, float, float]]:
    x = build_regime_matrix(close, atr_values)
    return predict_regime(regime_bundle, x[-1])
