"""Outcome-keyed reflection memory.

The LLM judgment loop currently sees the *chronologically* most recent
decisions. This module replaces "recent N" with "relevant N, bound to
realized P&L": it reconstructs closed round-trips from the `trades` table
(FIFO), computes each trip's realized return, and surfaces the lessons that
are *relevant to what we're about to trade* — same code first, then same
industry.

Deliberately read-only and dependency-free:
  * No new table, no write-hook into the broker — round-trips are derived
    from `trades` on demand.
  * No embeddings / vector store — "similarity" here is categorical
    (same code > same industry), which is the high-signal relevance for a
    single-name A-share book. A true embedding store is a later upgrade.
  * No LLM call — lessons are templated, so this adds zero token cost; it
    just makes the context the judgment/debate LLM already reads smarter.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


def _parse_date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def realized_trips(trades: list[dict]) -> list[dict]:
    """Reconstruct closed round-trips per code via FIFO lot matching.

    `trades` is as returned by `db.list_trades` (any order). Buys open lots;
    sells close them oldest-first. Each closed (sell) lot yields one trip with
    a realized return/P&L that nets buy/sell commissions into the cost/proceeds.

    Returns [{code, buy_date, sell_date, holding_days, realized_return,
    realized_pnl, qty}] where realized_pnl is the trip's yuan amount.
    """
    by_code: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_code[t["code"]].append(t)

    trips: list[dict] = []
    for code, ts in by_code.items():
        ts_sorted = sorted(
            ts, key=lambda r: (str(r.get("trade_date", "")),
                               str(r.get("created_at", ""))))
        lots: deque[list] = deque()  # [remaining_qty, unit_cost, buy_date]
        for t in ts_sorted:
            qty = int(t.get("quantity", 0) or 0)
            price = float(t.get("price", 0) or 0)
            comm = float(t.get("commission", 0) or 0)
            d = _parse_date(t.get("trade_date"))
            if qty <= 0 or price <= 0:
                continue
            per_share_comm = comm / qty if qty else 0.0
            if str(t.get("direction", "")).lower() == "buy":
                lots.append([qty, price + per_share_comm, d])
                continue
            # sell: match against oldest open lots
            sell_pps = price - per_share_comm
            remaining = qty
            matched_cost = 0.0
            matched_qty = 0
            earliest_buy: date | None = None
            while remaining > 0 and lots:
                lot = lots[0]
                take = min(remaining, lot[0])
                if earliest_buy is None:
                    earliest_buy = lot[2]  # FIFO → first lot is oldest
                matched_cost += take * lot[1]
                matched_qty += take
                lot[0] -= take
                remaining -= take
                if lot[0] == 0:
                    lots.popleft()
            if matched_qty > 0:
                avg_cost = matched_cost / matched_qty
                ret = (sell_pps - avg_cost) / avg_cost if avg_cost else 0.0
                days = (d - earliest_buy).days if (d and earliest_buy) else None
                trips.append({
                    "code": code, "buy_date": earliest_buy, "sell_date": d,
                    "holding_days": days, "realized_return": ret,
                    "realized_pnl": (sell_pps - avg_cost) * matched_qty,
                    "qty": matched_qty,
                })
    return trips


def build_reflections(
    db,
    candidates: list,
    *,
    max_items: int = 8,
) -> list[dict]:
    """Relevant, outcome-keyed lessons for the current candidate set.

    `candidates` may be FusedCandidate objects or dicts (need `code` and,
    optionally, `industry`). Returns up to `max_items` reflection dicts, each
    with a templated `text`, ranked code-level first, then industry-level,
    then by how notable the average outcome was.
    """
    # FIFO matching needs the FULL trade history: a recent-N window drops the
    # oldest buy legs once total trades exceed N, so in-window sells match
    # later lots (or nothing) and the avg/win-rate lessons come out wrong.
    try:
        trades = db.list_trades(limit=None)
    except Exception as e:
        logger.debug("list_trades failed: %s", e)
        return []
    if not trades:
        return []
    trips = realized_trips(trades)
    if not trips:
        return []

    # Current candidate codes + industries.
    cand_codes: list[str] = []
    cand_inds: set[str] = set()
    for c in candidates:
        code = getattr(c, "code", None) if not isinstance(c, dict) else c.get("code")
        if not code:
            continue
        cand_codes.append(code)
        ind = (getattr(c, "industry", "") if not isinstance(c, dict)
               else c.get("industry", "")) or ""
        if ind:
            cand_inds.add(ind)
    cand_code_set = set(cand_codes)

    trips_by_code: dict[str, list[dict]] = defaultdict(list)
    for t in trips:
        trips_by_code[t["code"]].append(t)

    items: list[dict] = []

    # Code-level: only for names we're considering right now.
    for code in cand_codes:
        ts = trips_by_code.get(code)
        if not ts:
            continue
        rets = [t["realized_return"] for t in ts]
        avg = sum(rets) / len(rets)
        last = sorted(ts, key=lambda x: x["sell_date"] or date.min)[-1]
        hold = (f", 持有 {last['holding_days']} 天"
                if last.get("holding_days") is not None else "")
        items.append({
            "scope": "code", "key": code, "n": len(ts),
            "avg_return": avg, "last_return": last["realized_return"],
            "relevance": 2,
            "text": (f"{code}: 历史已平仓 {len(ts)} 笔, 平均 {avg:+.1%}, "
                     f"最近一笔 {last['realized_return']:+.1%}{hold}"),
        })

    # Industry-level: aggregate trips whose code maps to a candidate industry.
    if cand_inds:
        ind_trips: dict[str, list[dict]] = defaultdict(list)
        ind_cache: dict[str, str] = {}
        for t in trips:
            code = t["code"]
            if code in cand_code_set:
                continue  # already covered at code level
            if code not in ind_cache:
                stock = db.get_stock(code)
                ind_cache[code] = (stock.industry if stock and stock.industry else "")
            ind = ind_cache[code]
            if ind in cand_inds:
                ind_trips[ind].append(t)
        for ind, ts in ind_trips.items():
            rets = [t["realized_return"] for t in ts]
            avg = sum(rets) / len(rets)
            win = sum(1 for r in rets if r > 0) / len(rets)
            items.append({
                "scope": "industry", "key": ind, "n": len(ts),
                "avg_return": avg, "last_return": None, "relevance": 1,
                "text": (f"{ind} 板块: 历史 {len(ts)} 笔, 平均 {avg:+.1%}, "
                         f"胜率 {win:.0%}"),
            })

    # Rank: relevance desc, then most-notable average outcome desc.
    items.sort(key=lambda x: (x["relevance"], abs(x["avg_return"])), reverse=True)
    return items[:max_items]


def format_reflections(items: list[dict]) -> str:
    return "\n".join(f"- {it.get('text', '')}" for it in items)
