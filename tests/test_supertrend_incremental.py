"""SuperTrend's incremental on_bar must emit bit-identical signals to the naive
from-scratch reference (the pre-optimization implementation, kept here as an
oracle). This locks the O(period)/bar rewrite against the original O(n²): a perf
change must not move a single signal (backtest≡live parity).
"""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from quanti.models import BarData, Direction

# strategies/ is a plugin dir, not an importable package — load by path.
_SPEC = importlib.util.spec_from_file_location(
    "supertrend_under_test",
    Path(__file__).resolve().parent.parent / "strategies" / "supertrend.py")
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)
SuperTrendStrategy = _MOD.SuperTrendStrategy


def naive_signals(bars, period, mult):
    """The pre-optimization from-scratch algorithm — reference oracle.

    Per bar, return 'BUY' / 'SELL' / None (the direction of the emitted signal,
    if any). This is byte-for-byte the logic the incremental version replaces.
    """
    highs, lows, closes, out = [], [], [], []
    for bar in bars:
        highs.append(bar.high)
        lows.append(bar.low)
        closes.append(bar.close)
        if len(closes) < period + 2:
            out.append(None)
            continue
        h = np.array(highs, float)
        lo = np.array(lows, float)
        c = np.array(closes, float)
        prev_c = np.concatenate(([c[0]], c[:-1]))
        tr = np.maximum(h - lo, np.maximum(np.abs(h - prev_c), np.abs(lo - prev_c)))
        atr = np.full(len(c), np.nan)
        for i in range(period - 1, len(c)):
            atr[i] = tr[i - period + 1 : i + 1].mean()
        hl2 = (h + lo) / 2.0
        upper = hl2 + mult * atr
        lower = hl2 - mult * atr
        f_up, f_lo = upper.copy(), lower.copy()
        direction = np.ones(len(c))
        start = period - 1
        for i in range(start + 1, len(c)):
            f_up[i] = upper[i] if (upper[i] < f_up[i - 1] or c[i - 1] > f_up[i - 1]) else f_up[i - 1]
            f_lo[i] = lower[i] if (lower[i] > f_lo[i - 1] or c[i - 1] < f_lo[i - 1]) else f_lo[i - 1]
            if c[i] > f_up[i - 1]:
                direction[i] = 1
            elif c[i] < f_lo[i - 1]:
                direction[i] = -1
            else:
                direction[i] = direction[i - 1]
        if direction[-2] < 0 and direction[-1] > 0:
            out.append("BUY")
        elif direction[-2] > 0 and direction[-1] < 0:
            out.append("SELL")
        else:
            out.append(None)
    return out


def incr_signals(bars, period, mult):
    """Per-bar 'BUY'/'SELL'/None from the production incremental strategy."""
    strat = SuperTrendStrategy()
    strat.init({"period": period, "multiplier": mult})
    out = []
    for bar in bars:
        sigs = strat.on_bar(bar)
        if not sigs:
            out.append(None)
        else:
            out.append("BUY" if sigs[0].direction == Direction.BUY else "SELL")
    return out


def _make_bars(closes, code="T"):
    d0 = date(2024, 1, 1)
    bars = []
    for k, c in enumerate(closes):
        c = float(c)
        bars.append(BarData(
            code=code, date=d0 + timedelta(days=k),
            open=c, high=c * 1.015, low=c * 0.985, close=c,
            volume=1_000.0, amount=c * 1_000.0))
    return bars


def _series():
    """A spread of price paths that exercise many flips (so the test actually
    covers the signal-emitting branches), not just monotone trends."""
    rng = np.random.default_rng(7)
    # oscillating — guarantees repeated long/short flips
    osc = 100 + 25 * np.sin(np.linspace(0, 8 * np.pi, 220)) + rng.normal(0, 2.0, 220)
    # random walk
    walk = 100 + np.cumsum(rng.normal(0, 1.5, 200))
    # strong uptrend then crash — flips near the turn
    trend = np.concatenate([np.linspace(50, 150, 120), np.linspace(150, 60, 80)])
    trend = trend + rng.normal(0, 1.0, len(trend))
    # near-flat (degenerate: few/no flips)
    flat = 100 + rng.normal(0, 0.3, 80)
    return {"osc": osc, "walk": walk, "trend": trend, "flat": flat}


def test_incremental_matches_naive_across_params_and_series():
    flips_seen = 0
    for name, closes in _series().items():
        bars = _make_bars(closes)
        for period in (7, 10, 14):
            for mult in (2.0, 3.0, 4.0):
                got = incr_signals(bars, period, mult)
                want = naive_signals(bars, period, mult)
                assert got == want, (
                    f"signal mismatch on series={name} period={period} "
                    f"mult={mult}:\n  incr={got}\n  naive={want}")
                flips_seen += sum(1 for s in got if s is not None)
    # Guard against a vacuous pass: the suite must actually hit BUY/SELL paths.
    assert flips_seen > 20, f"too few flips exercised ({flips_seen})"


def test_per_code_state_is_isolated():
    """Interleaving two codes must not cross-contaminate running state."""
    rng = np.random.default_rng(3)
    a = 100 + 20 * np.sin(np.linspace(0, 6 * np.pi, 150)) + rng.normal(0, 1.5, 150)
    b = 100 + np.cumsum(rng.normal(0, 1.2, 150))
    bars_a = _make_bars(a, code="AAA")
    bars_b = _make_bars(b, code="BBB")

    # Reference: each code run independently.
    ref_a = incr_signals(bars_a, 10, 3.0)
    ref_b = incr_signals(bars_b, 10, 3.0)

    # Interleaved through ONE strategy instance.
    strat = SuperTrendStrategy()
    strat.init({"period": 10, "multiplier": 3.0})
    got_a, got_b = [], []
    for ba, bb in zip(bars_a, bars_b):
        sa = strat.on_bar(ba)
        got_a.append(None if not sa else ("BUY" if sa[0].direction == Direction.BUY else "SELL"))
        sb = strat.on_bar(bb)
        got_b.append(None if not sb else ("BUY" if sb[0].direction == Direction.BUY else "SELL"))
    assert got_a == ref_a
    assert got_b == ref_b
