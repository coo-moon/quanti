"""Factor IC drift watcher.

rescore_generated_factors re-evaluates the generated-factor library against
current data every day and refreshes the `accepted` flag, but nothing records
the TRAJECTORY or tells an operator when a once-good edge has decayed. This
module turns the factor_ic_history snapshots into a decay decision:

  * baseline = mean OOS IC over the history EXCLUDING the recent window;
  * recent   = mean OOS IC over the last recent_n snapshots;
  * decayed  = baseline cleared the economic floor AND recent < ratio*baseline;
  * newly_rejected = the factor was gate-accepted at some earlier snapshot
    but the latest rescore rejected it — the library just retired it, so the
    operator should hear about it (and why);
  * unmonitored = an accepted factor with no snapshots yet (pre-feature rows):
    it trades on faith until the daily rescore fills its history.

Pure read-only + local; safe to run any time. Consumed by the daily bg-sync
hook (decision-log alert) and `quanti factor-watch` (CLI).
"""

from __future__ import annotations

import math

#: Decay means recent OOS IC fell below this FRACTION of the historical
#: baseline — half is a big drop for a daily-recomputed IC (regime noise
#: typically moves it 10-30%).
DEFAULT_DECAY_RATIO = 0.5
#: Only factors whose baseline cleared this economic floor can be "decayed";
#: a factor that never had real IC just stays rejected, not "decaying".
DEFAULT_MIN_BASELINE_IC = 0.02
#: Snapshots needed before a trajectory is judged at all.
DEFAULT_MIN_HISTORY = 6
#: The "recent" window = the last N snapshots (excluded from the baseline).
DEFAULT_RECENT_N = 3


def _mean_ic(snapshots: list[dict]) -> float | None:
    """Mean OOS IC over snapshots, ignoring NaN/None (untestable days)."""
    vals = [s["oos_ic"] for s in snapshots
            if s["oos_ic"] is not None and not math.isnan(s["oos_ic"])]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _chronological(history: list[dict]) -> list[dict]:
    """list_factor_ic_history returns newest-first; the watcher needs time.
    Returns a fresh ascending list."""
    return list(reversed(history))


def watch_factor_drift(db, *,
                       recent_n: int = DEFAULT_RECENT_N,
                       decay_ratio: float = DEFAULT_DECAY_RATIO,
                       min_history: int = DEFAULT_MIN_HISTORY,
                       min_baseline_ic: float = DEFAULT_MIN_BASELINE_IC) -> dict:
    """Per-factor decay report from the IC snapshot history.

    Returns {ok, factors: [{name, status, baseline, recent, latest_ic,
    latest_accepted, n}], decayed: [names], newly_rejected: [names],
    unmonitored: [names], as_of}. Never raises: a broken DB read yields an
    empty report with ok=False rather than killing the caller.
    """
    try:
        rows = db.list_generated_factors()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "factors": [], "decayed": [],
                "newly_rejected": [], "unmonitored": [],
                "as_of": None, "detail": f"因子库读取失败: {e}"}

    factors: list[dict] = []
    decayed: list[str] = []
    newly_rejected: list[str] = []
    newly_accepted: list[str] = []
    unmonitored: list[str] = []
    latest_date = None
    for row in rows:
        name = row["name"]
        try:
            hist = _chronological(db.list_factor_ic_history(name, limit=1000))
        except Exception as e:  # noqa: BLE001 - one factor cannot kill the watch
            factors.append({"name": name, "status": "error",
                            "detail": str(e), "n": 0})
            continue
        if not hist:
            if row["accepted"]:
                # Trades today on a gate decision we can no longer see. Until
                # the daily rescore fills snapshots this factor is on faith.
                unmonitored.append(name)
                factors.append({"name": name, "status": "unmonitored",
                                "baseline": None, "recent": None,
                                "latest_ic": None,
                                "latest_accepted": bool(row["accepted"]),
                                "n": 0})
            continue
        if hist[-1]["as_of"]:
            latest_date = hist[-1]["as_of"]
        n = len(hist)
        latest = hist[-1]
        earlier = hist[:-1]
        was_accepted = any(s["accepted"] for s in earlier)
        is_rejected = (not latest["accepted"]) and was_accepted
        is_newly_accepted = latest["accepted"] and not was_accepted

        entry: dict = {
            "name": name,
            "latest_ic": latest["oos_ic"],
            "latest_accepted": bool(latest["accepted"]),
            "n": n,
        }
        if n >= min_history:
            baseline = _mean_ic(hist[:-recent_n] if recent_n < n else [])
            recent = _mean_ic(hist[-recent_n:])
            entry["baseline"] = baseline
            entry["recent"] = recent
            is_decayed = (
                not is_newly_accepted
                and baseline is not None and baseline >= min_baseline_ic
                and recent is not None and recent < decay_ratio * baseline)
        else:
            entry["baseline"] = None
            entry["recent"] = None
            is_decayed = False

        if is_rejected:
            entry["status"] = "rejected"
            newly_rejected.append(name)
        elif is_newly_accepted:
            entry["status"] = "newly_accepted"
            newly_accepted.append(name)
        elif is_decayed:
            entry["status"] = "decayed"
            decayed.append(name)
        elif n < min_history:
            entry["status"] = "insufficient"
        else:
            entry["status"] = "healthy"
        factors.append(entry)

    return {
        "ok": not decayed and not newly_rejected and not unmonitored,
        "factors": factors,
        "decayed": decayed,
        "newly_rejected": newly_rejected,
        "newly_accepted": newly_accepted,
        "unmonitored": unmonitored,
        "as_of": latest_date.isoformat() if latest_date else None,
    }


def format_watch(report: dict) -> str:
    """Human-readable rendering for `quanti factor-watch`."""
    head = f"因子 IC 漂移 ({report.get('as_of') or '—'})"
    problems = (len(report.get("decayed", []))
                + len(report.get("newly_rejected", []))
                + len(report.get("unmonitored", [])))
    head += " — ⚠️ 需关注" if problems else " — ✅ 无衰减"
    lines = [head]
    for f in report.get("factors", []):
        if f["status"] in ("decayed", "rejected", "unmonitored"):
            bl = f"{f['baseline']:.3f}" if f.get("baseline") is not None else "—"
            rc = f"{f['recent']:.3f}" if f.get("recent") is not None else "—"
            ic = f"{f['latest_ic']:.3f}" if f.get("latest_ic") is not None else "—"
            lines.append(
                f"  ⚠️ {f['name']} [{f['status']}]: baseline={bl} "
                f"recent={rc} latest={ic} accepted={f['latest_accepted']} "
                f"n={f['n']}")
    if not lines[1:]:
        lines.append("  (无问题因子)")
    return "\n".join(lines)

