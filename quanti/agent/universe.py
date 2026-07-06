"""Tradable-universe filtering.

The naive "use all 5000 A-share codes" path is a trap:
  * ~5-10% are ST / *ST / delisting-warning names we never want to touch.
  * ~3-5% are new IPOs without enough history to score factors against.
  * The bottom 30% of stocks by ADV are so illiquid that a 50K 元 order
    moves their price more than its expected return.
  * 5000-stock walk-forward + factor-panel + signal generation costs 10×
    more compute than 1000-stock, with almost no actionable additional
    alpha (the gains hide in noise).

`UniverseBuilder` is the cleanup layer. It takes the raw stock list and
produces the subset that's actually worth evaluating each cycle:

  1. Stock-metadata filter (cheap, runs first):
     - age ≥ min_age_days (default 90)
     - not yet delisted as of the reference date (point-in-time via
       delist_date — keeps future-delisted names in historical replay)
     - name does NOT match `exclude_name_keywords` ("ST", "退" etc.) —
       LIVE path only; see `_filter_metadata` for why the current name is
       not point-in-time on historical replay.
  2. Liquidity filter (needs recent bars):
     - ADV20 ≥ min_adv20_yuan (default 5000 万)
     - active trading days in last 60 ≥ min_active_days_60 (default 40)

The result is cached on the AgentRuntime (per-day key) so a 5000 → 1000
filter doesn't run on every 4h tick — the inputs only change daily.

This module deliberately does NOT include factor scoring or strategy
ranking. Those happen downstream on the smaller filtered set, and are
the legitimate place to inject tick-level intelligence. This layer is
just "which stocks are tradeable at all".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from quanti.data.database import Database
from quanti.data.provider import DataProvider

logger = logging.getLogger(__name__)


@dataclass
class UniverseConfig:
    """Knobs for `UniverseBuilder`. All defaults tuned for daily A-share."""

    min_adv20_yuan: float = 50_000_000.0
    """5000万元 average daily turnover. Below this, slippage swamps alpha
    on retail-scale orders. Lower bound on what an institutional desk
    would consider 'investable'."""

    min_active_days_60: int = 40
    """Stocks with fewer than this many trading days of bars in the last 60
    are likely suspended, halted, or partially-listed. Drop them."""

    min_age_days: int = 90
    """IPOs in their first ~3 months don't have enough history for any
    of our factors to compute, and trade with extreme idiosyncratic
    volatility unrelated to the systematic patterns we're targeting."""

    exclude_name_keywords: tuple[str, ...] = ("ST", "退")
    """Substring match on stock NAME (not code). 'ST' catches *ST and ST*
    variants, '退' catches delisting names. Case-sensitive — A-share
    naming uses these exactly."""

    lookback_days: int = 90
    """How far back to load bars for ADV / active-days calculation.
    90 calendar days gives ~60 trading days of headroom."""


@dataclass
class UniverseFilterResult:
    """What survived each stage. Useful for debugging 'why is my list so small?'"""

    initial: int = 0
    after_metadata: int = 0
    after_liquidity: int = 0
    final: list[str] = field(default_factory=list)
    skipped_no_data: int = 0
    skipped_low_adv: int = 0
    skipped_inactive: int = 0

    def as_dict(self) -> dict:
        return {
            "initial": self.initial,
            "after_metadata": self.after_metadata,
            "after_liquidity": self.after_liquidity,
            "final": len(self.final),
            "skipped_no_data": self.skipped_no_data,
            "skipped_low_adv": self.skipped_low_adv,
            "skipped_inactive": self.skipped_inactive,
        }


class UniverseBuilder:
    """Filter raw stock list → tradable universe.

    Usage:
        builder = UniverseBuilder(db, provider)
        universe = builder.build()
        # Or with full attribution:
        universe, result = builder.build(return_details=True)
    """

    def __init__(self, db: Database, provider: DataProvider,
                 config: UniverseConfig | None = None) -> None:
        self._db = db
        self._provider = provider
        self._config = config or UniverseConfig()

    # ----- main entry -----

    def build(self, candidates: list[str] | None = None,
              as_of: date | None = None,
              return_details: bool = False,
              ) -> list[str] | tuple[list[str], UniverseFilterResult]:
        """Apply all filters in order. Returns the surviving codes.

        Args:
            candidates: If provided, filter just these codes. Otherwise
                start from the full stocks table.
            as_of: Reference date; defaults to today. The liquidity window
                ends here.
            return_details: If True, also return a UniverseFilterResult
                describing what happened at each stage.
        """
        as_of = as_of or date.today()
        result = UniverseFilterResult()

        if candidates is None:
            stocks = self._db.list_stocks()
            codes = [s.code for s in stocks]
        else:
            codes = list(candidates)
        result.initial = len(codes)

        codes = self._filter_metadata(codes, as_of)
        result.after_metadata = len(codes)

        codes = self._filter_liquidity(codes, as_of, result)
        result.after_liquidity = len(codes)
        result.final = codes

        logger.info(
            f"UniverseBuilder: {result.initial} → "
            f"{result.after_metadata} (metadata) → "
            f"{result.after_liquidity} (liquidity)"
        )

        if return_details:
            return codes, result
        return codes

    # ----- filters -----

    def _filter_metadata(self, codes: list[str], as_of: date) -> list[str]:
        """Stock-info-only filter. Cheap: one DB hit per code, no bars.

        Age and delisting are point-in-time; the ST/退 name rule cannot be:

          * Age: uses list_date (never changes) → always point-in-time.
          * Delisting: uses delist_date. A delisting is only *known* on and
            after that date, so a stock stays in the universe for any
            ``as_of < delist_date`` (it was actively trading then) and is
            dropped once ``as_of >= delist_date``. Same delist_date column
            `Database.point_in_time_universe` already trusts. This is the
            honest fix for the dominant survivorship leak: a currently
            "退"-named stock that was a normal, tradeable name at a past
            ``as_of`` must NOT be dropped just because it delisted later.
            Its delisting-cleanup crash is then captured for real, and once
            quotes stop the liquidity gate (min_active_days_60) evicts it —
            no look-ahead, no missed loss.
          * ST/退 name keywords: `stocks.name` is overwritten to the LATEST
            name on every `sync_stock_list` (upsert_stock uses
            ``ON CONFLICT DO UPDATE SET name=excluded.name``), so it is NOT
            point-in-time. On historical replay (``as_of`` in the past) we
            therefore do NOT filter on it — doing so drops future losers
            (optimistic) and keeps past-ST rebounders (also optimistic). We
            trust the name match only on the live path (``as_of >= today``),
            where the current name IS the point-in-time name.

        Residual: replay no longer excludes historically-ST names at all.
        Impact is small at the shipped cap (measured 0-2 of the top-100 ADV
        pool, 0 future-losers); it only reaches ~4% at cap>=500. A true
        point-in-time ST membership would need a namechange-backed name
        history, deliberately NOT built (rigor theater at cap<=100, and the
        namechange feed has its own PIT traps: NaN-end duplicate intervals,
        BJ code renumbering, off-by-one boundaries). `resolve_tradable_universe`
        warns when cap>300.
        """
        cfg = self._config
        is_live = as_of >= date.today()
        out: list[str] = []
        for code in codes:
            stock = self._db.get_stock(code)
            if stock is None:
                continue
            # Age check
            if stock.list_date and (as_of - stock.list_date).days < cfg.min_age_days:
                continue
            # Point-in-time delisting check (all paths). Drop once the
            # delisting is known (on/after delist_date), keep before.
            if stock.delist_date and as_of >= stock.delist_date:
                continue
            # Name-blocklist check — LIVE only (see docstring). We check NAME
            # not CODE because A-share ST status is reflected in the company
            # name (e.g. "*ST华源"), not the numeric code.
            if is_live and cfg.exclude_name_keywords and any(
                kw in (stock.name or "") for kw in cfg.exclude_name_keywords
            ):
                continue
            out.append(code)
        return out

    def _filter_liquidity(self, codes: list[str], as_of: date,
                          result: UniverseFilterResult) -> list[str]:
        """ADV + activity filter. Loads recent bars per code."""
        cfg = self._config
        start = as_of - timedelta(days=cfg.lookback_days)
        out: list[str] = []
        for code in codes:
            bars = self._provider.get_daily_bars(code, start, as_of)
            if not bars:
                result.skipped_no_data += 1
                continue
            # Active-days check uses bar count over the window — stocks
            # halted mid-window have fewer bars.
            if len(bars) < cfg.min_active_days_60:
                result.skipped_inactive += 1
                continue
            # ADV20: average daily turnover (in 元) over the most recent 20 bars.
            adv20 = self._adv20(bars)
            if adv20 < cfg.min_adv20_yuan:
                result.skipped_low_adv += 1
                continue
            out.append(code)
        return out

    @staticmethod
    def _adv20(bars: list) -> float:
        """Mean of `amount` over the last 20 bars (or whatever's available)."""
        recent = bars[-20:]
        if not recent:
            return 0.0
        amounts = [float(b.amount or 0) for b in recent]
        return sum(amounts) / len(amounts) if amounts else 0.0


# -------------------------------------------------------- helpers


def sort_by_adv20(provider: DataProvider, codes: list[str],
                  as_of: date | None = None,
                  lookback_days: int = 90) -> list[str]:
    """Sort `codes` descending by 20-day ADV. Used by the no-screener path
    so when the system needs to pick "the most tradeable N", it doesn't
    just take the first N by dictionary order.

    Codes with no recent bars or zero turnover sink to the bottom. The ADV of
    the whole universe is read in one batched query (see
    `Database.get_adv20_map`) rather than one round-trip per code — ranking
    ~5500 names dropped from ~22s to sub-second. `sorted` is stable, so codes
    with equal ADV keep their input order.
    """
    as_of = as_of or date.today()
    start = as_of - timedelta(days=lookback_days)
    adv = provider.get_adv20_map(start, as_of)
    return sorted(codes, key=lambda c: adv.get(c, 0.0), reverse=True)


def universe_config_from_params(params: dict | None) -> UniverseConfig:
    """Build a UniverseConfig from goal.params, falling back to defaults.

    Single source of truth for the `universe_min_*` knobs so the live agent
    (runtime) and the on-demand optimize / factor-mining paths read the same
    filter config and can't drift apart over time.
    """
    params = params or {}
    return UniverseConfig(
        min_adv20_yuan=float(params.get("universe_min_adv20",
                                        UniverseConfig.min_adv20_yuan)),
        min_active_days_60=int(params.get("universe_min_active_days",
                                          UniverseConfig.min_active_days_60)),
        min_age_days=int(params.get("universe_min_age_days",
                                    UniverseConfig.min_age_days)),
    )


def resolve_tradable_universe(
    db: Database,
    provider: DataProvider,
    *,
    pool: str | None,
    params: dict | None,
    as_of: date,
) -> list[str]:
    """Pick the codes an on-demand job (hyperopt / factor mining) should
    evaluate — the same selection the live agent uses, not a dictionary slice.

    The old path was `list_stocks()[:N]`, a dictionary-order slice (always
    000001, 000002, …) that ignored liquidity and tradeability, so results
    were measured on a non-representative low-quality sample. This aligns with
    `AgentRuntime._resolve_universe`:

      1. A user-curated `pool` is trusted and used as-is (no ST/IPO filter),
         mirroring the live agent's pool-trust.
      2. Otherwise start from all stocks; when `params["liquidity_filter"]`
         is on, run UniverseBuilder (drops ST / new IPOs / illiquid) as of
         `as_of` — same gate and same knobs as the live agent. If the filter
         would empty the list, fall back to unfiltered (never deadlock).
      3. Rank survivors by 20-day ADV (most-tradeable first) as of `as_of` and
         take the top `max(20, selector_max_universe)` (default 100). The
         floor preserves the old `max(20, N)` guard — never tune on a handful.

    `as_of` keeps the view point-in-time: optimizing over a past `end` ranks
    by liquidity *as known then*, and the metadata filter keeps names that had
    not yet delisted at `as_of` (via delist_date) while dropping the current
    ST/退 name match on replay (the stored name is the latest, not point-in-time).
    See `UniverseBuilder._filter_metadata` for the residual ST caveat.
    """
    params = params or {}
    cap = max(20, int(params.get("selector_max_universe", 100)))

    # Wide-cap survivorship tripwire. The ST/退 name filter is dropped on
    # historical replay (not point-in-time — see UniverseBuilder._filter_metadata),
    # leaving an uncorrected optimistic bias that is <1% of the top-100 ADV pool
    # but ~4% at cap>=500. Flag it so a wide-cap factor sweep feeding factor
    # adoption isn't read as clean. (Narrow default cap=100 is effectively unbiased.)
    if cap > 300:
        logger.warning(
            "tradable universe cap=%d (>300): residual current-name ST bias on "
            "replay is un-corrected (~4%% pollution at this width); treat wide-cap "
            "sweeps feeding factor adoption as optimistically biased.", cap)

    if pool:
        codes = [s.code for s in db.get_pool_stocks(pool)]
    else:
        codes = [s.code for s in db.list_stocks()]
        if bool(params.get("liquidity_filter", False)):
            cfg = universe_config_from_params(params)
            filtered = UniverseBuilder(db, provider, cfg).build(
                candidates=codes, as_of=as_of)
            if filtered:
                codes = filtered
            else:
                logger.warning(
                    "liquidity_filter dropped all %d codes — keeping unfiltered",
                    len(codes))

    ranked = sort_by_adv20(provider, codes, as_of=as_of)
    if len(ranked) > cap:
        logger.info("tradable universe: %d candidates → top %d by ADV",
                    len(ranked), cap)
    return ranked[:cap]
