"""Smoke tests for the borrowed strategies (supertrend / connors_rsi2 / kdj /
cci). Each asserts a BUY fires in an unambiguous designed scenario, plus the
loader discovers all of them with unique names (catches name collisions, which
would silently clobber DB/Goal keys)."""

from datetime import date, timedelta
from pathlib import Path

from quanti.models import BarData, Direction
from quanti.strategy.loader import StrategyLoader

STRAT_DIR = str(Path(__file__).resolve().parent.parent / "strategies")


def _bar(i: int, close: float, high: float | None = None, low: float | None = None) -> BarData:
    return BarData(
        code="X", date=date(2024, 1, 1) + timedelta(days=i),
        open=close, high=high if high is not None else close * 1.01,
        low=low if low is not None else close * 0.99,
        close=close, volume=1_000_000, amount=10_000_000,
    )


def _run(strat, closes):
    sigs = []
    for i, c in enumerate(closes):
        sigs += strat.on_bar(_bar(i, c))
    return sigs


def _has_buy(sigs):
    return any(s.direction == Direction.BUY for s in sigs)


def _load():
    return {s.name: s for s in StrategyLoader().load_directory(STRAT_DIR)}


def test_loader_discovers_all_with_unique_names():
    strats = _load()
    for name in ("supertrend", "connors_rsi2", "kdj_cross", "cci_reversion"):
        assert name in strats, f"{name} not discovered by loader"
    # 6 built-in + 4 borrowed; names are the DB/Goal key → must be unique.
    instances = StrategyLoader().load_directory(STRAT_DIR)
    names = [s.name for s in instances]
    assert len(names) == len(set(names)), f"duplicate strategy name: {names}"
    assert len(names) >= 10


def test_supertrend_buys_on_uptrend_flip():
    s = _load()["supertrend"]
    s.init({"period": 3, "multiplier": 1.0})
    down = [100 - 4 * i for i in range(10)]   # strong downtrend → dir = -1
    up = [down[-1] + 4 * i for i in range(1, 11)]  # strong reversal up → flip
    assert _has_buy(_run(s, down + up))


def test_connors_rsi2_buys_oversold_in_uptrend():
    s = _load()["connors_rsi2"]
    s.init({"rsi_period": 2, "buy_thresh": 50, "trend_ma": 10, "exit_ma": 5})
    closes = [10 + 2 * i for i in range(40)] + [87, 86]  # steep uptrend, 2-day dip
    assert _has_buy(_run(s, closes))


def test_kdj_golden_cross_buys():
    s = _load()["kdj_cross"]
    s.init({"period": 9, "oversold": 100, "overbought": 100})  # isolate the cross
    down = [100 - 3 * i for i in range(14)]   # drive K/D low, K below D
    up = [down[-1] + 6 * i for i in range(1, 6)]  # rebound → K crosses above D
    assert _has_buy(_run(s, down + up))


def test_cci_reversion_buys_crossing_up_through_lower():
    s = _load()["cci_reversion"]
    s.init({"period": 14, "upper": 100, "lower": -100})
    down = [100 - 3 * i for i in range(18)]   # CCI deep below -100
    up = [down[-1] + 5 * i for i in range(1, 8)]  # rebound crosses up through -100
    assert _has_buy(_run(s, down + up))
