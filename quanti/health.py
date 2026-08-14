"""Doctor — one-shot system health checks for operators and machines.

Answers "is the system healthy right now?" with three cheap, bounded,
read-only, network-free checks (safe to run any time without disturbing the
agent):

1. **exit coverage** — positions whose entry_strategy is no longer
   loadable from the strategies directory. Those holdings silently degrade
   to stop-loss / take-profit exits only (see quanti/execution/exits.py),
   which an operator must know about — it is usually caused by a strategy
   being retired into strategies/attic/.
2. **data freshness** — whether every code in the given universe (or all
   codes) has a daily bar through the latest expected trading day, per the
   trade calendar. Suspended names legitimately lag, so the check reports
   them rather than failing hard; a small stale tail is normal.
3. **DB integrity** — PRAGMA quick_check on the account and market
   SQLite files.

Used by:
* "quanti doctor" (CLI) — human-readable summary, non-zero exit on problems.
* GET /api/doctor (Web API) — machine-readable report (/api/health stays
  the cheap liveness probe).
* the background syncer daily hook — findings land in the decision log so
  problems surface in the audit trail without anyone having to remember to
  run the doctor.

Every function never raises: a broken check reports ok=False with a
detail instead of killing the caller.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

#: Codes are "stale" when their latest bar is this many calendar days (or
#: more) behind the latest expected trading day. Generous on purpose — it
#: only flags real gaps, not same-weekend noise.
DEFAULT_MAX_STALE_DAYS = 3


# ---------------------------------------------------------------- exit coverage
def load_strategy_names(strategies_dir: str) -> set[str]:
    """Names of every loadable strategy class under strategies_dir AND its
    attic — exit replay (exits.load_strategies) falls back to attic, so a
    holding whose strategy was retired there is NOT degraded."""
    from pathlib import Path
    try:
        from quanti.strategy.loader import StrategyLoader
        loader = StrategyLoader()
        names = {s.name for s in loader.load_directory(strategies_dir)}
        attic = Path(strategies_dir) / "attic"
        if attic.is_dir():
            names |= {s.name for s in loader.load_directory(str(attic))}
        return names
    except Exception as e:  # noqa: BLE001 - a broken dir must not kill the check
        logger.warning("strategy scan failed for %r: %s", strategies_dir, e)
        return set()


def exit_coverage(db, strategies_dir: str) -> dict:
    """Positions whose owning entry-strategy can no longer be loaded.

    Returns {ok, degraded, detail} where degraded is
    [{code, entry_strategy, name}] — the holdings that have lost their
    strategy-based exit and now only rely on stop-loss / take-profit.
    """
    try:
        positions = db.list_positions()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "degraded": [], "detail": f"持仓读取失败: {e}"}
    try:
        names = load_strategy_names(strategies_dir)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "degraded": [], "detail": f"策略目录扫描失败: {e}"}

    degraded: list[dict] = []
    for p in positions:
        strat = p.get("entry_strategy") or ""
        if not strat:
            continue  # manual / legacy entries have no owning strategy
        if strat not in names:
            code = p.get("code", "")
            name = ""
            try:
                stock = db.get_stock(code)
                name = stock.name if stock else ""
            except Exception:  # noqa: BLE001
                pass
            degraded.append({"code": code, "entry_strategy": strat,
                             "name": name})
    if degraded:
        detail = (f"{len(degraded)} 个持仓的入场策略已不在 strategies/attic,"
                  f"策略离场降级为仅止损/止盈:"
                  + "; ".join(f"{d['code']}({d['entry_strategy']})"
                              for d in degraded))
        return {"ok": False, "degraded": degraded, "detail": detail}
    return {"ok": True, "degraded": [], "detail": ""}


# ---------------------------------------------------------------- data freshness
def _expected_latest_trade_date(db, today: date | None = None) -> date | None:
    """Most recent calendar date <= today whose bar should exist. None
    when the trade calendar is empty (nothing to check against)."""
    today = today or date.today()
    try:
        dates = db.get_trade_dates(today - timedelta(days=14), today)
    except Exception as e:  # noqa: BLE001
        logger.warning("trade calendar read failed: %s", e)
        return None
    return dates[-1] if dates else None


def data_freshness(db, codes: list[str] | None = None,
                   max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
                   today: date | None = None) -> dict:
    """Compare each code's latest daily bar against the expected latest
    trading day. codes=None scans every stock in the DB.

    Returns {ok, expected, total, missing, stale, stale_sample, detail}.
    ok is True when no code is missing bars and the stale tail stays
    within a sane fraction (<5% of a non-trivial universe).
    """
    today = today or date.today()
    expected = _expected_latest_trade_date(db, today)
    if expected is None:
        return {"ok": True, "expected": None, "total": 0, "missing": 0,
                "stale": 0, "stale_sample": [],
                "detail": "交易日历为空,跳过新鲜度检查"}
    try:
        if codes is None:
            codes = [s.code for s in db.list_stocks()]
        latest = db.latest_quote_dates(codes)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "expected": expected.isoformat(), "total": 0,
                "missing": 0, "stale": 0, "stale_sample": [],
                "detail": f"行情读取失败: {e}"}

    missing: list[str] = []
    stale: list[tuple[str, int]] = []  # (code, days_behind)
    for code in codes:
        d = latest.get(code)
        if d is None:
            missing.append(code)
            continue
        behind = (expected - d).days
        if behind >= max_stale_days:
            stale.append((code, behind))
    stale.sort(key=lambda t: t[1], reverse=True)

    # A small stale tail is normal on big universes (suspended names), but
    # in a tiny universe any stale code matters. Fail when bars are missing,
    # or the stale tail exceeds both an absolute floor (5) and 5% of the
    # universe — so 1-of-1 stale fails while 50-of-5000 passes.
    ok = len(missing) == 0 and (
        len(codes) == 0 or len(stale) == 0
        or (len(stale) <= 5 and len(stale) <= len(codes) // 20))
    detail = (f"{expected.isoformat()} 应为最新交易日:"
              f" {len(codes)} 只中缺失 {len(missing)} 只、"
              f"落后 >= {max_stale_days} 天 {len(stale)} 只")
    return {
        "ok": ok,
        "expected": expected.isoformat(),
        "total": len(codes),
        "missing": len(missing),
        "stale": len(stale),
        "stale_sample": [{"code": c, "days_behind": b}
                         for c, b in stale[:10]],
        "missing_sample": missing[:10],
        "detail": detail,
    }


# ---------------------------------------------------------------- db integrity
def db_integrity(db) -> dict:
    """PRAGMA quick_check on account + market SQLite files."""
    try:
        result = db.integrity_check()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"完整性检查失败: {e}"}
    bad = {k: v for k, v in result.items() if v != "ok"}
    if bad:
        return {"ok": False, "detail": f"quick_check 异常: {bad}", "schemas": bad}
    return {"ok": True, "detail": "ok", "schemas": list(result)}


# ---------------------------------------------------------------- aggregate
def run_doctor(db, strategies_dir: str = "strategies",
               codes: list[str] | None = None) -> dict:
    """Run all checks; return a machine-readable report.

    {ok, checks: {exit_coverage, data_freshness, db_integrity}} — each
    check carries its own ok/detail. ok is the AND of all three.
    """
    checks: dict[str, dict] = {}
    for name, fn in (
        ("exit_coverage", lambda: exit_coverage(db, strategies_dir)),
        ("data_freshness", lambda: data_freshness(db, codes=codes)),
        ("db_integrity", lambda: db_integrity(db)),
    ):
        try:
            checks[name] = fn()
        except Exception as e:  # noqa: BLE001 - a broken check must not kill the doctor
            checks[name] = {"ok": False, "detail": f"检查崩溃: {e}"}
            logger.exception("doctor check %s crashed", name)
    ok = all(c.get("ok") for c in checks.values())
    return {"ok": ok, "checks": checks,
            "generated_at": date.today().isoformat()}


def format_doctor(report: dict) -> str:
    """Human-readable one-liner-per-check rendering for quanti doctor."""
    lines = [f"Quanti 体检 ({report['generated_at']}) — "
             f"{'✅ 全部通过' if report['ok'] else '⚠️ 发现问题'}"]
    for name, check in report["checks"].items():
        mark = "✅" if check.get("ok") else "⚠️"
        lines.append(f"  {mark} {name}: {check.get('detail', '')}")
    return "\n".join(lines)
