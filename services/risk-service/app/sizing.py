"""Position sizing wrapper - delegates ALL math to vix_core.risk.

The clamp-DOWN rule lives in :func:`vix_core.risk.compute_lots` and is
regression-tested there; this module only adapts service-level inputs
(signal levels, cached broker spec, account snapshot) to that call.
"""

from __future__ import annotations

from vix_core.risk import LotSizingResult, SymbolConstraints, compute_lots
from vix_core.schemas import Signal

from .validator import AccountSnapshot


def size_position(
    signal: Signal,
    *,
    account: AccountSnapshot,
    constraints: SymbolConstraints,
    risk_pct: float,
    margin_usage_cap: float = 0.5,
) -> LotSizingResult:
    """Risk-based lots for a signal; rejects below volume_min (no clamp-up).

    ``margin_usage_cap`` enforces the spec rule: required margin must not
    exceed cap * free margin (default 50%).
    """
    return compute_lots(
        equity=account.balance,
        risk_pct=risk_pct,
        entry=signal.entry,
        stop_loss=signal.sl,
        take_profit=signal.tp1,
        constraints=constraints,
        free_margin=account.margin_free,
        margin_buffer=margin_usage_cap,
    )
