"""Background inference worker: feature.computed -> Redis regime/meta caches."""

from __future__ import annotations

import asyncio
import json
import os
from typing import cast

import numpy as np
import redis.asyncio as aioredis
from redis.exceptions import ResponseError
from vix_core.config import Settings
from vix_core.correlation import (
    bind_correlation_id,
    get_or_create_correlation_id,
    unbind_correlation_id,
)
from vix_core.logging import get_logger

from .db import FeatureDatabaseML
from .hmm import ARTIFACT_PATH as HMM_PATH
from .hmm import load_regime_model, predict_regime
from .meta_label import (
    ARTIFACT_PATH as META_PATH,
)
from .meta_label import (
    build_meta_features,
    load_meta_model,
    predict_proba,
)

logger = get_logger(__name__)

STREAM_IN = "feature.computed"
INFERENCE_TIMEFRAMES: frozenset[str] = frozenset({"M15", "H1"})
REGIME_KEY = "regime:current"
META_KEY = "meta_label:current"


class InferenceWorker:
    """Subscribes to feature.computed; refreshes cached model outputs."""

    def __init__(
        self,
        settings: Settings,
        db: FeatureDatabaseML,
        redis: aioredis.Redis,
    ) -> None:
        self._settings = settings
        self._db = db
        self._redis = redis
        self._consumer = f"ml-{os.getpid()}"
        self.processed = 0
        self._regime_bundle: dict | None = None
        self._meta_bundle: dict | None = None

    # ------------------------------------------------------------------

    def _load_models(self) -> bool:
        try:
            self._regime_bundle = load_regime_model(HMM_PATH)
            self._regime_bundle["path"] = HMM_PATH
            self._meta_bundle = load_meta_model(META_PATH)
            self._meta_bundle["path"] = META_PATH
        except Exception:
            logger.warning("models not loaded yet; run POST /train first")
            return False
        else:
            return True

    async def run_forever(self) -> None:
        await self._ensure_group()
        logger.info("inference worker started", consumer=self._consumer)
        while True:
            try:
                if self._regime_bundle is None or self._meta_bundle is None:
                    await asyncio.to_thread(self._load_models)

                response = await self._redis.xreadgroup(
                    "ml-service",
                    self._consumer,
                    {STREAM_IN: ">"},
                    count=8,
                    block=5_000,
                )
            except asyncio.CancelledError:
                raise
            except ResponseError as exc:
                if "NOGROUP" in str(exc):
                    await asyncio.sleep(5)  # upstream group not created yet
                    continue
                logger.exception("worker stream error")
                await asyncio.sleep(2)
                continue
                logger.exception("worker stream error")
                await asyncio.sleep(2)
                continue
            except Exception:
                logger.exception("xreadgroup failed; retrying")
                await asyncio.sleep(2)
                continue

            for _stream, messages in response or []:
                for message_id, fields in messages:
                    await self._handle(message_id, fields)
                    await self._redis.xack(STREAM_IN, "ml-service", message_id)

    async def _ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(STREAM_IN, "ml-service", id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # ------------------------------------------------------------------

    async def _handle(self, message_id: bytes, fields: dict[bytes | str, bytes]) -> None:
        decoded = {
            (k.decode() if isinstance(k, bytes) else str(k)): (
                v.decode() if isinstance(v, bytes) else str(v)
            )
            for k, v in fields.items()
        }
        correlation_id = get_or_create_correlation_id(decoded)
        bind_correlation_id(correlation_id)
        try:
            timeframe = decoded.get("timeframe", "")
            symbol = decoded.get("symbol", "")
            if timeframe not in INFERENCE_TIMEFRAMES:
                return
            if self._regime_bundle is None or self._meta_bundle is None:
                logger.debug("skipping event; models unavailable")
                return

            meta_bundle = self._meta_bundle
            if meta_bundle is None:
                return
            row = await asyncio.to_thread(self._latest_feature_row, decoded)
            if row is None:
                return

            names, x = build_meta_features(
                cast("list[str]", meta_bundle["feature_names"]),
                cast(np.ndarray, row["matrix"]),
                regime_probs=cast(np.ndarray, row["probs"]),
                timestamps=cast(np.ndarray, row["ts"]),
            )
            del names  # layout persisted in artifact; kept for debugging

            regime_label, regime_state, probs = predict_regime(self._regime_bundle, x[-1])
            meta = predict_proba(self._meta_bundle, x[-1])

            await self._redis.set(
                REGIME_KEY,
                json_dumps(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "ts": row["ts_iso"],
                        "regime": regime_label,
                        "state": regime_state,
                        "probabilities": probs,
                        "correlation_id": correlation_id,
                    }
                ),
            )
            await self._redis.set(
                META_KEY,
                json_dumps({**{"symbol": symbol, "timeframe": timeframe}, **meta}),
            )
            self.processed += 1
            logger.info(
                "inference cached",
                timeframe=timeframe,
                regime=regime_label,
                p_up=meta["p_up"],
            )
        finally:
            unbind_correlation_id()

    def _latest_feature_row(self, decoded: dict[str, str]) -> dict[str, object] | None:
        """Pull the newest feature matrix window + HMM posteriors."""
        import numpy as np

        from .db import fetch_close_atr, fetch_feature_matrix

        dsn = self._settings.database_url
        symbol = decoded["symbol"]
        timeframe = decoded["timeframe"]

        ts_matrix, _columns, matrix = fetch_feature_matrix(dsn, symbol, timeframe, limit=64)
        if len(matrix) == 0:
            return None

        _, close, atr_values = fetch_close_atr(dsn, symbol, timeframe)
        regime_bundle = self._regime_bundle
        if regime_bundle is None:
            return None
        from .hmm import build_regime_matrix

        hmm_x = build_regime_matrix(close, atr_values)
        model_obj = regime_bundle["model"]
        probs = (
            model_obj.predict_proba(hmm_x[-len(matrix) :])
            if len(hmm_x) >= len(matrix)
            else np.zeros((len(matrix), 3))
        )
        # Align lengths conservatively from the tail.
        n = min(len(matrix), len(probs))
        latest_ts = ts_matrix[-1:]
        return {
            "ts": latest_ts,
            "ts_iso": str(latest_ts[0]),
            "matrix": matrix[-n:],
            "probs": probs[-n:],
        }


def json_dumps(payload: dict) -> str:
    return json.dumps(payload, default=str)
