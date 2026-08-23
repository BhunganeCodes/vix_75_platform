"""Visualization HTTP routes."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request
from vix_core.config import Settings
from vix_core.logging import get_logger

from .db import VizDatabase

logger = get_logger(__name__)
router = APIRouter()


def get_db(request: Request) -> VizDatabase:
    return cast(VizDatabase, request.app.state.db)


def get_settings_dep(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


@router.get("/health")
async def health(db: VizDatabase = Depends(get_db)) -> dict[str, object]:
    return {
        "service": "visualization-service",
        "status": "ok" if await db.ping() else "degraded",
    }


@router.get("/api/charts/strategy")
async def strategy_chart(
    db: VizDatabase = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    timeframe: str = Query(default="M15"),
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    from .charts import render_strategy_chart

    ohlcv = await db.fetch_ohlcv(settings.symbol, timeframe, days)
    signals = await db.fetch_signals(settings.symbol, days)

    # Flatten zones from the latest feature row that has them.
    features = await db.fetch_features_with_regime(settings.symbol, timeframe, days)
    zones_raw: list[dict[str, Any]] = []
    zones_col = features.get("zones") or []
    for z in reversed(zones_col):
        if isinstance(z, list) and z:
            zones_raw = [zi for zi in z if isinstance(zi, dict)]
            break

    return render_strategy_chart(
        ohlcv,
        zones_raw,
        [
            {k: str(v) if not isinstance(v, (int, float)) else v for k, v in s.items()}
            for s in _dicts(signals)
        ],
    )


@router.get("/api/charts/regime")
async def regime_chart(
    db: VizDatabase = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    timeframe: str = Query(default="M15"),
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    from .charts import render_regime_chart

    ohlcv = await db.fetch_ohlcv(settings.symbol, timeframe, days)
    features = await db.fetch_features_with_regime(settings.symbol, timeframe, days)
    return render_regime_chart(ohlcv, features)


@router.get("/api/charts/ml_features")
async def ml_explainability(
    db: VizDatabase = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    """Feature importance + P(win) timeline."""

    from vix_core.artifacts import load_artifact

    from app.charts import render_ml_explainability

    importance: list[tuple[str, float]] = []
    try:
        bundle: dict[str, Any] = load_artifact(  # type: ignore[assignment]
            "models/meta_label_lgbm.joblib"
        )
        model_obj: Any = bundle["model"]
        names: list[str] = bundle.get("feature_names", [])
        importances: list[float] = [
            float(v) for v in getattr(model_obj, "feature_importances_", [])
        ]
        importance = sorted(
            zip(names, importances),
            key=lambda pair: pair[1],
            reverse=True,
        )[:20]
    except Exception:
        logger.warning("could not load meta-model for explainability")

    # P(win) timeline from audit_log or cached values - simplified to use
    # the latest single value until a proper history table exists.
    proba_timeline: dict[str, list[Any]] = {"ts": [], "p_up": []}

    return render_ml_explainability(importance, proba_timeline)


_DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<title>VIX75 Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
body {{ background:#0d1117; color:#e0e0e0; font-family:monospace; }}
#charts div {{ margin-bottom:20px; }}
</style>
</head>
<body>
<h1>VIX75 Trading Dashboard</h1>
<div id="charts">
  <div id="strategy"></div>
  <div id="regime"></div>
  <div id="ml"></div>
</div>
<script>
const token = new URLSearchParams(window.location.search).get('token') || '';
const opts = {{ headers: {{ 'Authorization': 'Bearer ' + token }} }};
const target = '/api/charts';

Promise.all([
  fetch(target+'/strategy', opts).then(r => r.json()),
  fetch(target+'/regime', opts).then(r => r.json()),
  fetch(target+'/ml_features', opts).then(r => r.json()),
]).then(([strategy, regime, ml]) => {{
  Plotly.newPlot('strategy', strategy.data, strategy.layout);
  Plotly.newPlot('regime', regime.data, regime.layout);
  Plotly.newPlot('ml', ml.data, ml.layout);
}});
</script>
</body>
</html>"""


@router.get("/dashboard")
async def dashboard() -> Any:
    """Serve the interactive HTML dashboard wrapper."""
    html_content = _DASHBOARD_TEMPLATE.format()
    from fastapi.responses import HTMLResponse

    return HTMLResponse(content=html_content, status_code=200)


def _dicts(data: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(data.keys())
    n = len(data[keys[0]]) if keys else 0
    return [{k: data[k][i] for k in keys} for i in range(n)]
