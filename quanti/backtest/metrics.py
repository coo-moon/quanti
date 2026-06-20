"""Backtest performance metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd

# Below this many bars, annualizing extrapolates too aggressively (e.g. a
# ~2-week window has exponent 252/14 ≈ 18, turning +20% into 900%+). Such
# figures dominated walk-forward strategy selection. For short windows we
# report the un-extrapolated cumulative return as `annual_return` instead.
_MIN_DAYS_TO_ANNUALIZE = 60


def compute_metrics(
    equity_curve: pd.Series,
    risk_free_rate: float = 0.03,
    trading_days: int = 252,
) -> dict:
    """Compute performance metrics from an equity curve."""
    returns = equity_curve.pct_change().dropna()

    total_return = (equity_curve.iloc[-1] / equity_curve.iloc[0]) - 1
    n_days = len(equity_curve)
    if n_days >= _MIN_DAYS_TO_ANNUALIZE:
        annual_return = (1 + total_return) ** (trading_days / n_days) - 1
    else:
        annual_return = total_return  # don't extrapolate a tiny window

    # Volatility
    annual_vol = returns.std() * np.sqrt(trading_days) if len(returns) > 1 else 0.0

    # Max drawdown
    cummax = equity_curve.cummax()
    drawdown = (equity_curve - cummax) / cummax
    max_drawdown = drawdown.min()

    # Sharpe ratio
    excess_daily = returns.mean() - risk_free_rate / trading_days
    sharpe = (excess_daily / returns.std() * np.sqrt(trading_days)) if returns.std() > 0 else 0.0

    # Sortino ratio
    downside = returns[returns < 0]
    downside_std = downside.std() if len(downside) > 1 else 0.0
    sortino = (
        (excess_daily / downside_std * np.sqrt(trading_days)) if downside_std > 0 else 0.0
    )

    # Calmar ratio
    calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": annual_vol,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "win_rate": (returns > 0).mean() if len(returns) > 0 else 0.0,
        "trading_days": n_days,
    }
