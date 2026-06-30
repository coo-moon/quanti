"""SuperTrend Strategy (ATR-adaptive trend follower).

Borrowed from freqtrade/jesse community strategies. A volatility-adaptive trend
line: bands = (high+low)/2 ± multiplier*ATR with the classic band-lock recursion,
flipping long/short as price crosses it. Distinct from the MA-cross / Donchian /
Bollinger strategies already built in — the band width scales with volatility.
Long-only here (BUY on flip-up, SELL on flip-down); short side dropped.
"""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class SuperTrendStrategy(BaseStrategy):
    """Buy when price flips above the SuperTrend line, sell when it flips below.

    Incremental by construction. SuperTrend's bands and direction are a causal
    recursion — each bar depends only on the *previous* final band + the current
    bar — so we carry that state per code and advance it ONE step per on_bar:
    O(period) per bar, not the O(n²) of re-deriving the whole history every bar
    (which the from-scratch version did, also growing memory without bound).
    State only ever moves forward from past bars, so look-ahead stays impossible.
    Signals are bit-for-bit identical to the from-scratch version — locked by
    tests/test_supertrend_incremental.py's pairwise check against a naive oracle.
    """

    name = "supertrend"
    name_zh = "超级趋势"
    description = "ATR 自适应趋势线:收盘上穿翻多买入、下穿翻空卖出(轨宽随波动放缩)"
    param_space = {"period": [7, 10, 14], "multiplier": [2.0, 3.0, 4.0]}

    def init(self, config: dict) -> None:
        self.period = config.get("period", 10)
        self.multiplier = config.get("multiplier", 3.0)
        # Per-code running state: rolling TR window (for the SMA-ATR), the prior
        # close (for TR + the band-lock condition), and the previous bar's final
        # upper/lower band + direction.
        self._state: dict[str, dict] = {}

    def on_bar(self, bar: BarData) -> list[Signal]:
        st = self._state.get(bar.code)
        if st is None:
            st = {"i": 0, "tr": [], "prev_close": None,
                  "fup": float("nan"), "flo": float("nan"), "dir": 1}
            self._state[bar.code] = st

        period, mult = self.period, self.multiplier
        i = st["i"]
        h, lo, c, pc = bar.high, bar.low, bar.close, st["prev_close"]

        # True range. First bar has no prior close → TR = high-low, matching the
        # from-scratch version which seeded prev_close[0] = close[0] (the other
        # two TR terms can't exceed high-low when close ∈ [low, high]).
        tr = (h - lo) if pc is None else max(h - lo, abs(h - pc), abs(lo - pc))
        st["tr"].append(tr)
        if len(st["tr"]) > period:
            st["tr"].pop(0)  # keep only the last `period` TRs

        fup_prev, flo_prev, dir_prev = st["fup"], st["flo"], st["dir"]
        signals: list[Signal] = []

        if i < period - 1:
            # ATR warm-up: bands undefined, direction holds the initial +1.
            fup_i, flo_i, dir_i = float("nan"), float("nan"), 1
        else:
            atr = sum(st["tr"]) / period            # SMA of the last `period` TRs
            hl2 = (h + lo) / 2.0
            upper = hl2 + mult * atr
            lower = hl2 - mult * atr
            if i == period - 1:
                # First ATR bar (start): seed the bands; direction still +1, no
                # recursion yet (from-scratch starts its loop at start+1).
                fup_i, flo_i, dir_i = upper, lower, 1
            else:
                # Band-lock recursion, exactly one step, off the previous finals.
                fup_i = upper if (upper < fup_prev or pc > fup_prev) else fup_prev
                flo_i = lower if (lower > flo_prev or pc < flo_prev) else flo_prev
                if c > fup_prev:
                    dir_i = 1
                elif c < flo_prev:
                    dir_i = -1
                else:
                    dir_i = dir_prev
                # A flip is only meaningful once ≥ period+2 bars exist (i.e.
                # i ≥ period+1); the from-scratch version returned [] below that.
                if i >= period + 1:
                    if dir_prev < 0 and dir_i > 0:
                        signals.append(Signal(
                            stock_code=bar.code, direction=Direction.BUY,
                            strength=0.8,
                            reason=f"SuperTrend 翻多 (ATR{period}×{mult})"))
                    elif dir_prev > 0 and dir_i < 0:
                        signals.append(Signal(
                            stock_code=bar.code, direction=Direction.SELL,
                            strength=0.8,
                            reason=f"SuperTrend 翻空 (ATR{period}×{mult})"))

        st["fup"], st["flo"], st["dir"] = fup_i, flo_i, dir_i
        st["prev_close"] = c
        st["i"] = i + 1
        return signals
