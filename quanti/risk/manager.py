"""Risk management module.

Two-layer drawdown architecture
--------------------------------
Layer 1 — **protections** (``quanti/risk/protections.py``):
    MaxDrawdown soft-lock. Window peak-to-trough; threshold default -8%.
    Locks *new BUY entries* for K trading days after any trigger day.
    Does NOT force a sell.

Layer 2 — **portfolio_stop_loss** (this module, ``check_portfolio_stop``):
    All-time HWM hard breaker. Default -15% from the running high-water mark.
    Flattens all positions and halts the agent when triggered.
    Threshold: ``RiskConfig.portfolio_stop_loss_pct``.

Layer 1 fires first and is softer; Layer 2 is the last resort.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date

from quanti.models import Direction, Portfolio, Signal

STOP_LOSS_REASON_PREFIX = "止损"
"""Single source of truth for the stop-loss exit reason prefix. `check_exits`
emits it; protections identify stop-loss exits by it (plus strategy_name
'risk_exit'). Changing the wording here keeps producer and consumer in sync."""

STRATEGY_EXIT_REASON_PREFIX = "策略离场"
"""Single source of truth for the strategy-exit reason prefix. `check_exits`
emits ``{prefix}信号 (strategy)``. All risk exits share strategy_name
'risk_exit', so the reason TEXT is the only thing distinguishing stop-loss vs
strategy-exit vs take-profit — audit/UI match on this prefix (cf. the stop-loss
LIKE query in database.py)."""

DRIFT_TRIM_STRATEGY = "drift_trim"
"""strategy_name tag for concentration-trim (削峰) partial sells. The ONLY sell
path that honors a sub-1.0 signal.strength — every other SELL (stop / TP /
strategy-exit / manual / flatten) sells the full sellable qty regardless of
strength. The fill paths gate the partial-sell primitive on this tag, so a
strategy that emits a closing SELL with strength<1.0 still fully exits."""


@dataclass
class RiskConfig:
    """Risk management configuration."""

    max_position_pct: float = 0.20  # Max 20% per stock (UI-adjustable, persisted in risk_config)
    max_industry_pct: float = 0.30  # Max 30% per industry (UI-adjustable, persisted in risk_config)
    stop_loss_pct: float = -0.15
    """Absolute per-stock stop FLOOR — the widest a single-stock stop may ever
    be. ATR (atr_stop_k>0) is the primary stop and tightens on top of this; a
    stop can never be wider than this floor, so it's the last-resort backstop
    even when ATR is off or its data is missing. (No longer a flat one-size
    -8% line — the 5y sweep showed flat -8% was the worst; ATR + floor replaced
    it.)"""
    portfolio_stop_loss_pct: float = -0.30  # -30% portfolio drawdown circuit breaker
    max_daily_trades: int = 20
    # (ST/*ST filtering is NOT here — it lives in agent/universe.py by stock
    # NAME. A code-prefix blocklist here was dead config: never read, and ST is
    # a name prefix not a code prefix, so it couldn't match anyway. Removed to
    # avoid advertising an inert safety limit — audit G7.)

    # --- Exit overlays (see check_exits) ---
    take_profit_activate_pct: float = 0.15
    """Arm the trailing take-profit once a position is up at least this much.
    Below it, only the stop-loss governs. 0 disables take-profit entirely."""
    take_profit_trail_pct: float = 0.10
    """Once armed, exit if the price retraces this fraction from its post-entry
    peak. Lets winners run but locks in gains on a meaningful reversal."""
    strategy_exit_enabled: bool = True
    """Exit a holding when its owning entry-strategy emits a SELL on the
    latest bar (structure-based exit, coherent with why we bought)."""
    atr_stop_k: float = 2.0
    """ATR-adaptive stop multiplier — the PRIMARY per-stock stop. Default 2.0:
    a 5y sweep showed ATR k=2 beats a flat line across trend + mean-reversion
    strategies (sharpe -0.96 vs -1.24). When >0 the stop is -k·(ATR/price),
    tightened for calm names and widened for volatile ones, but never wider
    than the stop_loss_pct floor (max(floor, -k·ratio)). 0 disables ATR → the
    stop_loss_pct floor alone governs. The caller injects a per-code ATR/price
    ratio (volatility, dimensionless → adjust-agnostic) into check_exits."""
    atr_stop_n: int = 14
    """Lookback (trading days) for the ATR behind atr_stop_k."""

    # --- Extreme gap-up entry guard ---
    extreme_gap_up_block_pct: float = 0.10
    """Abandon a pending BUY when the fill price gaps up >= this fraction above
    the prior close (chasing a blow-off open). A 5y study showed the >= +10%
    gap-up bucket is a lottery (median T+5 ≈ -4.4%, p5 ≈ -20%, negative mean in
    the recent regime) and waiting for a pullback has no edge. Near-no-op on the
    main board (a +10% gap is a limit-up open the tradability gate already
    blocks); it primarily protects the 20cm/30cm boards, where mean forward
    return flips negative at exactly 10%. 0 disables. SELLs are never blocked."""

    # --- Concentration trim (削峰) — opt-in, default OFF ---
    drift_trim_enabled: bool = False
    """When True, a holding whose weight drifts above
    drift_trim_to_pct×(1+drift_trim_band) is PARTIALLY sold back down to the
    drift_trim_to_pct edge. ONE-SIDED (trim-only; never tops up an underweight
    name). Pure concentration/drawdown control, NOT an alpha/trend bet — it only
    caps single-name concentration. Keep OFF unless a concrete concentration
    problem exists: the ~万10 round-trip cost otherwise outweighs the benefit
    (and quanti's research says passive equal-weight already wins)."""
    drift_trim_to_pct: float = 0.10
    """Trim an over-weight holding back down to this fraction of equity (the
    band's lower edge — trade-to-edge, not to a tighter target)."""
    drift_trim_band: float = 0.25
    """No-trade band: only trigger a trim once weight exceeds
    drift_trim_to_pct×(1+band) (e.g. 0.10×1.25 = 12.5%). MUST stay wide —
    Davis-Norman's cube-root law: even a small per-trade cost needs a band
    several % wide before trimming pays. A tight band bleeds cost to churn."""

    # --- Score-gated rotation (换仓) — opt-in, default OFF ---
    rotation_enabled: bool = False
    """When the book is too full to fund a new buy from cash, sell the weakest
    holding to free room for a clearly-stronger fresh candidate. Only fires in
    the scored paths (ensemble / LLM) and at most ONCE per cycle (churn guard).
    OFF by default: quanti's research says churn bleeds cost and passive
    equal-weight wins — chasing relative attractiveness was not a reliable OOS
    edge, so this is a safety valve for "good name shows up but I'm full", not a
    return enhancer."""
    rotation_margin: float = 0.15
    """Min final_score (∈[0,1]) advantage a newcomer must have over the weakest
    holding to displace it. Higher = stricter / less churn. A holding that isn't
    a candidate this cycle scores 0, so any candidate ≥ margin can displace it."""


def risk_config_from_dict(overrides: dict) -> RiskConfig:
    """Build a RiskConfig from a (partial) dict of runtime overrides — fields
    absent (or None) keep their dataclass defaults. Lets brokers read the
    runtime risk_config table live so edits apply without a restart (P0-3)."""
    cfg = RiskConfig()
    known = {f.name for f in fields(cfg)}
    for k, v in (overrides or {}).items():
        if k in known and v is not None:
            setattr(cfg, k, v)
    return cfg


class RiskManager:
    """Independent risk control layer between signals and execution."""

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self._daily_trade_count = 0
        # Calendar day the count belongs to. Live/paper never call reset_daily(),
        # so the count auto-rolls when the real date changes — otherwise the cap
        # became a permanent lifetime lock after `max_daily_trades` trades.
        self._count_day: date | None = None

    def _roll_day_if_needed(self) -> None:
        """Reset the daily counter when the calendar day has changed."""
        today = date.today()
        if self._count_day != today:
            self._daily_trade_count = 0
            self._count_day = today

    def check(self, signal: Signal, portfolio: Portfolio) -> tuple[bool, str]:
        """Check if a signal passes risk rules. Returns (allowed, reason)."""
        self._roll_day_if_needed()
        # Always allow sells
        if signal.direction == Direction.SELL:
            return True, ""

        # Per-stock concentration tripwire. The total-position (80%) cap was
        # intentionally removed — full deployment is allowed, bounded only by
        # the per-stock 10% / per-industry 30% caps, which are the real
        # post-trade enforcement point in max_additional_buy_value.
        total_value = portfolio.total_value
        if signal.stock_code in portfolio.positions:
            pos = portfolio.positions[signal.stock_code]
            if total_value > 0:
                stock_ratio = pos.market_value / total_value
                if stock_ratio >= self.config.max_position_pct:
                    return False, f"Position in {signal.stock_code} at {stock_ratio:.1%} exceeds limit {self.config.max_position_pct:.1%}"

        # Check daily trade limit
        if self._daily_trade_count >= self.config.max_daily_trades:
            return False, f"Daily trade limit ({self.config.max_daily_trades}) reached"

        return True, ""

    def max_additional_buy_value(self, portfolio: Portfolio, code: str,
                                 industry: str = "") -> float:
        """Largest 元 value addable to `code` without breaching the single-stock
        or industry caps — computed POST-trade (room up to the ceiling, net of
        what's already held). 0.0 when a cap is already at its limit. This is the
        real enforcement point for the hard caps; callers size buys against it.
        Pass `industry=""` to skip the industry cap (e.g. when industry data
        isn't available, as in the backtest). The total-position (80%) cap was
        removed — full deployment is bounded only by these per-name caps."""
        total = portfolio.total_value
        if total <= 0:
            return 0.0
        cfg = self.config
        held = portfolio.positions.get(code)
        stock_mv = held.market_value if held else 0.0
        stock_room = total * cfg.max_position_pct - stock_mv
        if industry:
            ind_mv = sum(p.market_value for p in portfolio.positions.values()
                         if p.industry == industry)
            ind_room = total * cfg.max_industry_pct - ind_mv
        else:
            ind_room = float("inf")
        return max(0.0, min(stock_room, ind_room))

    def check_portfolio_stop(self, total_value: float, peak_value: float) -> bool:
        """True when equity has drawn down from its high-water mark past
        `portfolio_stop_loss_pct` (e.g. -15%). Portfolio-level circuit breaker
        — the caller flattens everything and halts the agent."""
        if peak_value <= 0:
            return False
        return ((total_value - peak_value) / peak_value
                <= self.config.portfolio_stop_loss_pct)

    def _effective_stop_pct(self, atr_ratio: float | None) -> tuple[float, bool]:
        """The stop threshold (a negative pnl%) and whether ATR drives it. ATR
        (atr_stop_k>0) tightens on top of the stop_loss_pct floor:
        max(floor, -k·ratio). Single source for check_exits + stop_info."""
        cfg = self.config
        stop_pct = cfg.stop_loss_pct
        if cfg.atr_stop_k > 0 and atr_ratio is not None and atr_ratio > 0:
            stop_pct = max(cfg.stop_loss_pct, -cfg.atr_stop_k * atr_ratio)
        return stop_pct, stop_pct != cfg.stop_loss_pct

    def stop_info(self, avg_cost: float, atr_ratio: float | None = None) -> dict:
        """The price a holding would stop out at: avg_cost·(1+stop_pct). For the
        live status card. `atr_ratio` = ATR(atr_stop_n)/price for the code."""
        stop_pct, atr_driven = self._effective_stop_pct(atr_ratio)
        return {
            "stop_pct": stop_pct,
            "stop_price": round(avg_cost * (1 + stop_pct), 3) if avg_cost > 0 else 0.0,
            "atr_driven": atr_driven,
        }

    def check_exits(
        self,
        portfolio: Portfolio,
        peaks: dict[str, float] | None = None,
        strategy_sell_codes: set[str] | dict[str, str] | None = None,
        atr_ratios: dict[str, float] | None = None,
    ) -> list[Signal]:
        """Decide which holdings to close. One SELL per code.

        Exit priority (固化 — do not reorder; locked by test_exit_priority):
            1. stop-loss   (always on; ATR-adaptive when atr_stop_k>0)
            2. strategy-exit  (owning strategy flipped to SELL)
            3. trailing take-profit

        NOTE this is the per-stock layer. The portfolio circuit-breaker
        (check_portfolio_stop, -15% from the equity high-water mark) is a
        SEPARATE, higher-priority mechanism the caller runs first — when it
        trips it flattens everything, so in effect the full order is
        circuit-breaker > stop-loss > strategy-exit > take-profit.

        Price basis (固化 — 收盘确认型 on the daily-tick/backtest path):
          • pos.current_price is the latest CLOSE (EOD mark) on the 4h tick
            and in backtests — stops evaluated on the close, never intraday.
            EXCEPTION: the intraday guard (live via xtdata; paper via Tencent
            marks, PaperBroker._intraday_marks) feeds realtime prices here,
            making stops intraday-touch on that path — a deliberate
            live-fidelity divergence from the backtest's close-confirmed model
            (a name that pierces the stop intraday exits that day at the
            realtime price even if the close recovers).
          • peaks[code] is the post-entry highest HIGH (intraday) since entry.
            So the trailing TP measures an intraday peak vs a close retrace by
            design (a deliberate close-confirmed trail), NOT a bug.
          • atr_ratios[code] is ATR(atr_stop_n)/price — a dimensionless
            volatility ratio, so it's adjust-agnostic (hfq vs raw both give the
            same number); the stop derived from it is therefore comparable to
            pos.pnl_pct regardless of price adjustment.

        Pure logic — caller injects peaks / strategy_sell_codes / atr_ratios.
        All optional, so existing callers keep working (degrade to fixed
        stop-loss only).
        """
        peaks = peaks or {}
        strategy_sell_codes = strategy_sell_codes or set()
        atr_ratios = atr_ratios or {}
        cfg = self.config
        signals: list[Signal] = []
        for code, pos in portfolio.positions.items():
            # 1. Stop-loss — highest priority, always on. ATR (atr_stop_k>0) is
            #    the primary stop, floored at stop_loss_pct: a stop is never
            #    wider than the floor, so the floor is the absolute backstop
            #    when ATR is off or its ratio is missing.
            stop_pct, atr_driven = self._effective_stop_pct(atr_ratios.get(code))
            if pos.pnl_pct <= stop_pct:
                tag = f" (ATR×{cfg.atr_stop_k:g})" if atr_driven else " (地板)"
                signals.append(Signal(
                    stock_code=code, direction=Direction.SELL, strength=1.0,
                    reason=f"{STOP_LOSS_REASON_PREFIX} {pos.pnl_pct:.1%} ≤ {stop_pct:.1%}{tag}"))
                continue
            # 2. Strategy-coherent exit — the owning strategy flipped to SELL.
            if cfg.strategy_exit_enabled and code in strategy_sell_codes:
                # dict input carries the owning strategy name → put it in the
                # reason so the audit shows WHICH strategy said sell; a plain
                # set (older callers / tests) degrades to no name.
                src = (strategy_sell_codes.get(code) or ""
                       if isinstance(strategy_sell_codes, dict) else "")
                tag = f" ({src})" if src else ""
                signals.append(Signal(
                    stock_code=code, direction=Direction.SELL, strength=1.0,
                    reason=f"{STRATEGY_EXIT_REASON_PREFIX}信号{tag}"))
                continue
            # 3. Trailing take-profit — armed above activate, exit on retrace.
            if cfg.take_profit_activate_pct > 0 and pos.pnl_pct >= cfg.take_profit_activate_pct:
                peak = peaks.get(code)
                if peak and pos.current_price > 0:
                    drawdown = (pos.current_price - peak) / peak
                    if drawdown <= -cfg.take_profit_trail_pct:
                        signals.append(Signal(
                            stock_code=code, direction=Direction.SELL, strength=1.0,
                            reason=(f"移动止盈 浮盈{pos.pnl_pct:+.1%} 自峰值回撤"
                                    f"{drawdown:.1%}")))
        return signals

    def check_drift_trims(self, portfolio: Portfolio,
                          exclude: set[str] | None = None) -> list[Signal]:
        """One-sided concentration trim (削峰). For each holding whose weight has
        drifted above drift_trim_to_pct×(1+band), emit a PARTIAL SELL whose
        `strength` is the fraction to shave to bring it back to the
        drift_trim_to_pct edge (trade-to-edge). Trim-only — never tops up an
        underweight name. Opt-in (drift_trim_enabled); empty when off.

        `exclude` = codes already being FULLY exited this cycle (stop/TP/strategy
        sells) so a name isn't both flattened and trimmed. Pure risk/turnover
        control: it reads only current weights, forecasts nothing."""
        cfg = self.config
        if not cfg.drift_trim_enabled or cfg.drift_trim_to_pct <= 0:
            return []
        exclude = exclude or set()
        eq = portfolio.total_value
        if eq <= 0:
            return []
        edge = cfg.drift_trim_to_pct
        trigger = edge * (1.0 + cfg.drift_trim_band)
        out: list[Signal] = []
        for code, pos in portfolio.positions.items():
            if code in exclude:
                continue
            w = pos.market_value / eq
            if w > trigger:
                frac = min(max((w - edge) / w, 0.0), 1.0)  # shave back to edge
                if frac > 0:
                    out.append(Signal(
                        stock_code=code, direction=Direction.SELL, strength=frac,
                        reason=f"削峰 权重{w:.1%}>{trigger:.1%}→回{edge:.0%}"))
        return out

    def reset_daily(self) -> None:
        """Reset daily counters. Backtest calls this per simulated day; live/
        paper rely on the auto-roll in `_roll_day_if_needed`."""
        self._daily_trade_count = 0
        self._count_day = date.today()

    def seed_daily_trades(self, count: int) -> None:
        """Seed today's open-trade count from an authoritative source (the
        broker's own trades at session start). A live process restart mid-day
        otherwise resets ``_daily_trade_count`` to 0, letting max_daily_trades be
        bypassed by re-launching (audit G2). Rolls the day first so the seed
        lands on today; never lowers an already-higher count."""
        self._roll_day_if_needed()
        self._daily_trade_count = max(self._daily_trade_count, int(count))

    def record_trade(self, direction: Direction | None = None) -> None:
        """Count a trade against the daily cap. The cap limits NEW positions
        (opens) — exits (SELL), including forced stop-loss / flatten, do NOT
        consume it; otherwise a day with a cluster of stop-losses would burn the
        budget and block the rebalancing BUYs exactly when flexibility is most
        needed (audit F2). Callers pass the fill direction; `None` counts as an
        open for backward compatibility."""
        self._roll_day_if_needed()
        if direction == Direction.SELL:
            return
        self._daily_trade_count += 1
