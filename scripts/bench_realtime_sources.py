"""实时行情源基准:腾讯 qt.gtimg.cn vs tushare(sina)—— 稳定性 + RT。

逐轮绕过两个模块的 TTL 缓存与失败退避(否则测的是缓存命中,不是源),
对同一批代码交替打两源,统计:成功率、延迟分位(p50/p90/max)、覆盖率
(返回码数/请求码数)、最长连续失败,以及两源重叠代码的价格一致性
(应同为 raw 最新价,偏差大 = 有源在发旧价)。

用法:
    python scripts/bench_realtime_sources.py                 # 默认 30 轮 × 2s
    python scripts/bench_realtime_sources.py --rounds 60 --interval 5
    python scripts/bench_realtime_sources.py --codes 600519,000001,601328

注意:这是主动打源的压测脚本,轮距默认 2s 已留余量;盘中跑更有代表性
(盘后两源都只回当日最后成交,新鲜度过滤仍生效)。
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import time
from pathlib import Path

from quanti.data import tencent_quotes, tushare_quotes

DEFAULT_CODES = ["600519", "000001", "601328", "601288", "000703",
                 "601919", "300750", "000506", "600036", "601888"]


def _token() -> str:
    """env TUSHARE_TOKEN → cwd data/paper.db → 脚本所在仓的 data/paper.db。
    (worktree 里跑时 cwd 常无交易库,按顺序找。)"""
    import os
    tok = os.environ.get("TUSHARE_TOKEN", "").strip()
    if tok:
        return tok
    candidates = [Path.cwd() / "data" / "paper.db",
                  Path(__file__).resolve().parent.parent / "data" / "paper.db"]
    for db in candidates:
        if not db.exists():
            continue
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT data_source_token FROM app_config WHERE id=1"
            ).fetchone()
            tok = (row[0] or "") if row else ""
            if tok:
                return tok
        finally:
            conn.close()
    return ""


def _reset_caches() -> None:
    """绕过 TTL 缓存与失败退避——每轮都真实打源。"""
    tencent_quotes._cache = None
    tencent_quotes._last_fail = None
    tushare_quotes._cache = None
    tushare_quotes._last_fail = None


def _round(fetch, codes: list[str]) -> tuple[bool, float, dict[str, float], str]:
    t0 = time.perf_counter()
    try:
        out = fetch(codes) or {}
        return bool(out), (time.perf_counter() - t0) * 1000, out, ""
    except Exception as e:  # noqa: BLE001 - 基准要记录而非中断
        return False, (time.perf_counter() - t0) * 1000, {}, f"{type(e).__name__}: {e}"


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--codes", type=str, default="")
    args = ap.parse_args()

    codes = ([c.strip() for c in args.codes.split(",") if c.strip()]
             or DEFAULT_CODES)
    token = _token()
    if not token:
        print("!! paper.db 无 tushare token,tushare 侧将全失败(仅测腾讯)")

    stats = {name: {"lat": [], "ok": 0, "cov": [], "streak": 0,
                    "max_streak": 0, "errors": {}}
             for name in ("tencent", "tushare")}
    diffs: list[float] = []

    print(f"基准开始: {len(codes)} 码 × {args.rounds} 轮 × {args.interval}s 轮距")
    for i in range(args.rounds):
        _reset_caches()
        results = {}
        for name, fetch in (
            ("tencent", lambda c: tencent_quotes.fetch_last_prices(c)),
            ("tushare", lambda c: tushare_quotes.fetch_last_prices(c, token)),
        ):
            ok, ms, out, err = _round(fetch, codes)
            s = stats[name]
            s["lat"].append(ms)
            results[name] = out
            if ok:
                s["ok"] += 1
                s["cov"].append(len(out) / len(codes))
                s["streak"] = 0
            else:
                s["streak"] += 1
                s["max_streak"] = max(s["max_streak"], s["streak"])
                if err:
                    key = err[:60]
                    s["errors"][key] = s["errors"].get(key, 0) + 1
        both = set(results["tencent"]) & set(results["tushare"])
        for c in both:
            a, b = results["tencent"][c], results["tushare"][c]
            if a > 0:
                diffs.append(abs(a - b) / a)
        line = " | ".join(
            f"{n}: {'✓' if results[n] else '✗'} "
            f"{stats[n]['lat'][-1]:5.0f}ms {len(results[n]):2d}/{len(codes)}码"
            for n in ("tencent", "tushare"))
        print(f"[{i + 1:3d}/{args.rounds}] {line}")
        if i < args.rounds - 1:
            time.sleep(args.interval)

    print("\n===== 汇总 =====")
    for name in ("tencent", "tushare"):
        s = stats[name]
        n = args.rounds
        cov = statistics.mean(s["cov"]) * 100 if s["cov"] else 0.0
        print(f"{name:8s} 成功 {s['ok']}/{n} ({s['ok'] / n:.0%})  "
              f"延迟 p50={_pct(s['lat'], 0.50):.0f}ms "
              f"p90={_pct(s['lat'], 0.90):.0f}ms "
              f"max={max(s['lat']):.0f}ms  "
              f"覆盖 {cov:.0f}%  最长连败 {s['max_streak']}")
        for err, cnt in sorted(s["errors"].items(), key=lambda kv: -kv[1]):
            print(f"         err×{cnt}: {err}")
    if diffs:
        print(f"两源价格一致性: 重叠样本 {len(diffs)},"
              f"平均偏差 {statistics.mean(diffs):.4%},"
              f"最大偏差 {max(diffs):.4%}"
              + ("  ⚠️ 偏差偏大,查谁在发旧价" if max(diffs) > 0.005 else "  ✓"))
    else:
        print("两源无重叠成功样本,无法比价")


if __name__ == "__main__":
    main()
