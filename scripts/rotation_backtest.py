#!/usr/bin/env python3
"""分数门换仓(rotation)验证回测 —— 同一段历史跑「OFF vs ON」,看换仓是否亏手续费。

用法:.venv/bin/python scripts/rotation_backtest.py [--start ... --end ... --recent N --margin 0.15]

为什么不用 agent_backtest.py:它每月「整体重选 top-K」、无持仓延续,结构上演示不出
「仓位满、好票来了动不了」的活体动态,也就测不出换仓的边际效果。本脚本改成持仓延续模型:
  - K 个名额(等权),持仓跨月延续;
  - 退出(两臂相同):止损粘性 —— 持仓一直拿到「自建仓以来收益跌破止损 floor(默认 -15%)」
    才卖。这是你担心的场景:赢家被长期持有、book 填满后卡住,正是换仓要解的问题。
    (不按 score<阈值 退——那等于每月整体重选,book 永不卡满,换仓无从触发。)
  - 换仓(仅 ON 臂):名额满且某新候选 final_score 高出最弱持仓 ≥ margin → 换(直接调用
    生产函数 quanti.agent.signal_pipeline.select_rotation_sells,测的是真逻辑);
  - 补仓(两臂相同):有空名额就用分数最高的新候选填满。
顺序对齐 live:退出 → 换仓 → 补仓。每笔买卖按 COST_PER_TURN 计换手成本。

简化(直说,免误读;与 agent_backtest 同源假设):月度等权手算净值、止损按调仓点收益判定
(月内插针未建模);只建模止损这一条退出(live 还有策略离场/止盈会更早腾位、从而进一步
摊薄换仓作用)→ 这是「book 最粘」的压力测试。但「OFF vs ON 同口径对比」恰好隔离出换仓的
边际收益/成本,手续费账目精确计入。PIT 正确:collect_signals_per_strategy(end=rd)、
compute_factor_panel(as_of=rd)。行情读 market.db;临时 account 库可删,不碰 paper/live。
"""
import sys
import time
import argparse
from datetime import date, timedelta

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")  # 复用 agent_backtest 的助手(零重复)
import agent_backtest as ab  # noqa: E402
from quanti.data.database import Database  # noqa: E402
from quanti.data.provider import DataProvider  # noqa: E402
from quanti.factors.cross_sectional import compute_factor_panel  # noqa: E402
from quanti.agent.signal_pipeline import (  # noqa: E402
    collect_signals_per_strategy, fuse_buy_signals, select_rotation_sells)
from quanti.strategy.loader import StrategyLoader  # noqa: E402

K = ab.TOP_K              # 名额数(等权)
MAX_POS = 1.0 / K         # 单名额权重 = book-full gate 的「一个满仓位」
THRESHOLD = ab.THRESHOLD
COST = ab.COST_PER_TURN
STOP = -0.15              # 止损 floor(= RiskConfig.stop_loss_pct 默认),粘性退出阈值


def fused_scores(provider, db, loader, candidates, rd):
    """{code: final_score} —— rd 当日在候选池上的融合分(PIT)。"""
    if not candidates:
        return {}
    strats = ab.fresh_strategies(loader)
    pairs = [(s, 1.0 / len(strats)) for s in strats]
    per_strat, weights = collect_signals_per_strategy(
        pairs, candidates, provider, end=rd)
    panel = compute_factor_panel(provider, db, candidates, as_of=rd)
    fused = fuse_buy_signals(per_strat, weights,
                             factor_panel=panel, factor_blend=ab.FACTOR_BLEND)
    return {c.code: c.final_score for c in fused}


def close_on(provider, code, rd):
    """rd 当日(或之前最近一根)的 close;无数据返回 None。"""
    bars = provider.get_daily_bars(code, rd - timedelta(days=12), rd)
    return bars[-1].close if bars else None


def precompute_scores(provider, db, loader, rebal):
    """每月融合分(与 margin/rotation 无关)只算一次 → {rd: {code: final_score}}。

    这是回测的成本大头(每月 4 策略 on_bar + 因子面板),OFF 臂和各 margin 的 ON 臂全复用,
    把 margin 扫描从「N×全程」压到「1×打分 + N×几乎免费的记账」。
    """
    out = {}
    for rd in rebal:
        t = time.time()
        out[rd] = fused_scores(provider, db, loader,
                               ab.liquidity_pool(provider, rd, ab.N_CAND), rd)
        print(f"  打分 {rd}: {len(out[rd])} 只 ({time.time()-t:.1f}s)", flush=True)
    return out


def simulate(provider, scores_by_date, rebal, *, rotation, margin):
    """持仓延续 + 止损粘性 + (可选)换仓。返回统计。

    每个调仓点 rd:① 止损退出(自建仓收益 ≤ STOP);② 换仓(仅 ON,名额满+过门);
    ③ 用最强新候选补满空位。然后结算上月 book 在 [prev_rd, rd] 的等权收益、扣换手成本。
    """
    nav = 1.0
    rets, turns = [], []
    total_rot = 0
    book: list[str] = []
    entry_close: dict[str, float] = {}   # code → 建仓 close(止损基准,跨月保留)
    prev_book, prev_rd = None, None
    for rd in rebal:
        scores = scores_by_date[rd]
        # ① 止损退出(粘性):只卖跌破 floor 的,赢家继续持有 → book 变粘、会填满。
        survivors = []
        for c in book:
            ec, cc = entry_close.get(c), close_on(provider, c, rd)
            if ec and cc and (cc / ec - 1.0) <= STOP:
                continue
            survivors.append(c)
        book = survivors
        fresh = sorted((c for c in scores if c not in book and scores[c] >= THRESHOLD),
                       key=lambda c: scores[c], reverse=True)
        # ② 换仓(仅 ON):名额满 + 新候选高出最弱 ≥ margin → 调用生产函数定夺。
        if rotation and fresh:
            held_mv = {c: MAX_POS for c in book}
            cash = (K - len(book)) * MAX_POS         # 空名额 = 现金
            sells = select_rotation_sells(
                fresh, scores, held_mv, cash, 1.0,
                margin=margin, max_position_pct=MAX_POS, max_rotations=1)
            for s in sells:
                book.remove(s.stock_code)
                total_rot += 1
        # ③ 补仓:空名额用分数最高的新候选填满。
        for c in fresh:
            if len(book) >= K:
                break
            if c not in book:
                book.append(c)
        # 更新建仓基准:老仓保留原 entry(止损从真实成本算),新仓记 rd 的 close。
        entry_close = {c: (entry_close.get(c) or close_on(provider, c, rd))
                       for c in book}
        # 结算上月
        if prev_book:
            r, _ = ab.period_returns(provider, prev_book, prev_rd, rd)
            turn = 1.0 - len(set(prev_book) & set(book)) / max(len(prev_book), 1)
            r *= (1 - turn * COST)
            nav *= (1 + r)
            rets.append(r)
            turns.append(turn)
        prev_book, prev_rd = book, rd
    s = ab.stats(rets)
    s["nav"] = nav
    s["avg_turnover"] = float(sum(turns) / len(turns)) if turns else 0.0
    s["n_rotations"] = total_rot
    return s


def main():
    ap = argparse.ArgumentParser(description="分数门换仓验证回测 (OFF vs ON)")
    ap.add_argument("--start", default="2021-08-01")
    ap.add_argument("--end", default="2026-06-24")
    ap.add_argument("--recent", type=int, default=0, help="只跑最近N个月(小测)")
    ap.add_argument("--margins", default="0.10,0.15,0.20,0.25",
                    help="逗号分隔的换仓分数门扫描值")
    ap.add_argument("--account-db", default=ab.ACCOUNT_DB)
    ap.add_argument("--market-db", default=ab.MARKET_DB)
    args = ap.parse_args()
    margins = [float(x) for x in args.margins.split(",") if x.strip()]

    db = Database(args.account_db, market_db_path=args.market_db)
    db.initialize()
    provider = DataProvider(db)
    loader = {s.name: s for s in StrategyLoader().load_directory("strategies")}

    rebal = ab.monthly_rebal_dates(provider, date.fromisoformat(args.start),
                                   date.fromisoformat(args.end))
    if args.recent:
        rebal = rebal[-args.recent:]
    print(f"调仓月数={len(rebal)} 起{rebal[0]} 止{rebal[-1]} K={K} "
          f"margins={margins} 单边成本={COST:.1%}", flush=True)

    t = time.time()
    scores_by_date = precompute_scores(provider, db, loader, rebal)
    print(f"打分完成 ({time.time()-t:.0f}s),开始扫描", flush=True)

    # OFF 与 margin 无关,只跑一次;各 margin 复用同一份打分。
    off = simulate(provider, scores_by_date, rebal, rotation=False, margin=0.0)
    on = {m: simulate(provider, scores_by_date, rebal, rotation=True, margin=m)
          for m in margins}

    def line(tag, s):
        return (f"{tag}: 总{s['total']*100:+6.1f}% 年化{s['ann']*100:+6.2f}% "
                f"夏普{s['sharpe']:+.2f} 回撤{s['mdd']*100:+6.1f}% "
                f"月均换手{s['avg_turnover']*100:4.0f}% 换仓{s['n_rotations']:>3}次")
    print("\n=== 结果(同一份打分,OFF 基准 + margin 扫描)===")
    print(line("OFF       ", off))
    for m in margins:
        print(line(f"ON m={m:<4}", on[m]))
    print("\n=== 换仓净效应(ON − OFF, 已扣成本)===")
    for m in margins:
        d = on[m]["ann"] - off["ann"]
        print(f"  margin={m:<4}: 年化{d*100:+.2f}%  "
              f"换手{(on[m]['avg_turnover']-off['avg_turnover'])*100:+.0f}pp/月  "
              f"换仓{on[m]['n_rotations']}次")
    # 决策相关的判定是「有没有 margin 亏手续费(净效应<0)」,不是「是否处处严格>OFF」
    # ——margin 过高时换仓归零、净效应=0(退回 OFF),那是关掉、不是亏。
    worst = min(on[m]["ann"] - off["ann"] for m in margins)
    best_m = max(margins, key=lambda m: on[m]["ann"])
    if worst >= -1e-9:
        print(f"判定: 各 margin 净效应均 ≥0(无亏手续费),峰值 margin={best_m}")
    else:
        print(f"判定: 存在负净效应的 margin(亏手续费)→ 谨慎")
    db.close()


if __name__ == "__main__":
    main()
