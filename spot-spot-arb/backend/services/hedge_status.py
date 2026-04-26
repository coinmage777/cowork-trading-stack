"""Shared hedge status classification utilities."""

from __future__ import annotations

import math
from typing import Any

from backend import config

_QTY_EPSILON = 1e-8


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def classify_hedge_status(
    spot_qty: float,
    futures_qty: float,
    futures_price_usdt: float | None = None,
) -> dict[str, float | str | None]:
    """Classify hedge status with relative and notional residual tolerances."""
    spot = max(_safe_float(spot_qty) or 0.0, 0.0)
    futures = max(_safe_float(futures_qty) or 0.0, 0.0)

    residual_qty = spot - futures
    residual_abs = abs(residual_qty)
    base_qty = max(spot, futures, _QTY_EPSILON)
    residual_ratio = residual_abs / base_qty

    price = _safe_float(futures_price_usdt)
    residual_notional_usd = residual_abs * price if price is not None and price > 0 else None

    ratio_tolerance = float(getattr(config, 'HEDGE_RESIDUAL_RATIO_TOLERANCE', 0.001) or 0.001)
    ratio_tolerance = max(ratio_tolerance, 0.0)
    notional_tolerance = float(
        getattr(config, 'HEDGE_RESIDUAL_NOTIONAL_USD_TOLERANCE', 1.0) or 1.0
    )
    notional_tolerance = max(notional_tolerance, 0.0)

    within_ratio = residual_ratio <= ratio_tolerance
    within_notional = (
        residual_notional_usd is None
        or residual_notional_usd <= notional_tolerance
    )
    hedged = residual_abs <= _QTY_EPSILON or (within_ratio and within_notional)

    return {
        'status': 'hedged' if hedged else 'partial_hedged',
        'residual_qty': residual_qty,
        'residual_ratio': residual_ratio,
        'residual_notional_usd': residual_notional_usd,
        'hedge_ratio_tolerance': ratio_tolerance,
        'hedge_notional_tolerance_usd': notional_tolerance,
    }
