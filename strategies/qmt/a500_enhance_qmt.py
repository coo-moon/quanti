# coding: utf-8
# ---------------------------------------------------------------------------
# CSI A500 index-enhanced strategy — QMT executor (策略编辑器格式 init/handlebar)
#
# 职责边界: 本文件只做"执行", 不做选股。目标持仓由 quanti 侧
#   scripts/a500_signals.py 每月末收盘后生成 (a500_target.csv, 原子写),
#   本策略在次日开盘按 csv 权重调仓。因子逻辑只有一份实现(与回测一致),
#   QMT 端不重算因子 => 不存在两套实现漂移。
#
# 部署:
#   1) QMT 客户端(miniQMT 模式)策略编辑器新建 python 策略, 粘贴本文件;
#   2) 改 CSV_PATH / ACCOUNT_ID / MAX_CAPITAL;
#   3) 周期选 日K, 运行方式"实盘"或先"模拟"。
#   4) quanti 侧 cron: 每月最后交易日 16:00 跑 scripts/a500_signals.py,
#      并把 data/a500_target.csv 同步到 CSV_PATH (共享盘/scp)。
#
# 护栏(与仓内 RiskManager/实盘红线一致):
#   - 信号过期熔断: csv 的 execute_from 距今超过 MAX_STALE_DAYS 个自然日 => 拒绝交易并告警
#   - 极端高开熔断: 开盘相对昨收 gap >= +10% 的票当期跳过买入 (仓内默认, PR #121)
#   - 单股权重上限 10% 在信号侧已作用; 执行侧再兜底 clip
#   - 涨停不追买 / 跌停不砸卖
#   - 总投入受 MAX_CAPITAL 限制, 不满仓打光 (预留 CASH_BUFFER)
#   - 幂等: 按 目标-现状 差分下单, 重复触发不会重复买
#
# 回测依据: scripts/a500_backtest.py (fund 因子7个: 估值/股息/规模/质量/成长,
# 行业中性, top-50, band=2)。数字与红队裁决见 docs/2026-07-16-a500-enhance.md:
# 头条超额经对抗验证后被推翻(超额主体=2025-26 AI 行情的市值加权集中度, 跨
# regime 样本为负, honest DSR 0.40), 前瞻超额按 0~负 理解。本执行器仅供
# >=400万资金按"A500 贝塔+期望≈0的tilt"谨慎使用; 小资金触发可行性护栏会拒绝
# 交易(正确动作=直接买 A500 ETF)。
#
# 除 init/handlebar 外的纯逻辑函数不依赖 QMT, 可直接自测:
#   python strategies/qmt/a500_enhance_qmt.py --selftest
# 标注 # VERIFY 的行是 QMT API 细节, 上真机时对照本机 xtquant 版本核对一次。
# ---------------------------------------------------------------------------

import csv
import io
import os
from datetime import datetime, date

# ----------------------------------------------------------------- 用户配置
CSV_PATH = r"C:\quanti\a500_target.csv"   # a500_target.csv 同步到 QMT 机器的路径
ACCOUNT_ID = "REPLACE_ME"                 # miniQMT 资金账号
MAX_CAPITAL = 200_000.0                   # 本策略最大投入(元) — 资金上限护栏
CASH_BUFFER = 0.01                        # 留 1% 现金缓冲防超买
MAX_STALE_DAYS = 7                        # 信号超龄天数(自然日), 超过拒绝交易
GAP_FUSE = 0.10                           # 极端高开熔断阈值(+10%)
MIN_LOT = 100                             # A股一手
MAX_SINGLE_WEIGHT = 0.10                  # 单股权重兜底上限
MIN_TRADE_VALUE = 2_000.0                 # 小于该金额的调整忽略(防碎单)
MIN_FEASIBLE_WEIGHT = 0.60                # 可行目标权重占比低于此值 => 拒绝交易
# ^ 红队裁决(2026-07): 资金太小时高价股(如寒武纪一手~30万)连一手都买不起,
#   小账户实际跑的是另一个策略且带反动量偏差; 忠实复制本策略需 >=400万资金。
#   可行权重(每只 1手成本<=目标市值 的权重之和)<60% 时宁可不交易也不跑走样版。


# ------------------------------------------------------------ 纯逻辑(可自测)
def parse_target_csv(text):
    """解析 a500_target.csv → (meta dict, [(qmt_code, weight)]).

    首行: as_of,<date>,execute_from,<date>,top_k,...  次行: 表头  再后: 数据行。
    权重兜底 clip 到 MAX_SINGLE_WEIGHT 后重归一。
    """
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        raise ValueError("target csv 行数不足")
    head = rows[0]
    meta = {head[i]: head[i + 1] for i in range(0, len(head) - 1, 2)}
    cols = rows[1]
    ic, iw = cols.index("qmt_code"), cols.index("weight")
    targets = [(r[ic], float(r[iw])) for r in rows[2:] if len(r) > max(ic, iw)]
    if not targets:
        raise ValueError("target csv 无持仓行")
    clipped = [(c, min(w, MAX_SINGLE_WEIGHT)) for c, w in targets]
    tot = sum(w for _, w in clipped)
    if tot <= 0:
        raise ValueError("target csv 权重和为 0")
    return meta, [(c, w / tot) for c, w in clipped]


def signal_fresh(meta, today, max_stale_days=MAX_STALE_DAYS):
    """execute_from ≤ today ≤ execute_from+max_stale 才允许执行。"""
    try:
        exec_from = datetime.strptime(meta["execute_from"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return False, "target csv 缺 execute_from"
    if today < exec_from:
        return False, "未到执行日 %s" % exec_from
    if (today - exec_from).days > max_stale_days:
        return False, "信号已过期 %s 天(>%d), 请重跑 a500_signals.py" % (
            (today - exec_from).days, max_stale_days)
    return True, ""


def plan_orders(targets, positions, prices, prev_closes, capital,
                gap_fuse=GAP_FUSE, min_lot=MIN_LOT, min_trade_value=MIN_TRADE_VALUE):
    """目标权重 vs 当前持仓 → [(code, 'buy'|'sell', shares)]。

    targets:   [(code, weight)]
    positions: {code: 可用股数(可卖)};  prices: {code: 现价};
    prev_closes: {code: 昨收} — 计算开盘 gap 熔断;
    capital:   本策略计划总市值(含现有持仓市值)。
    卖出全排在买入前(腾资金)。停牌(无价)跳过。
    """
    orders, skipped = [], []
    tw = dict(targets)
    # 可行性护栏: 一手成本 > 目标市值的股票买不起(整手制), 只能剔除并记录跟踪缺口;
    # 可行权重占比过低说明资金规模撑不起该策略 => 由调用方拒绝交易(fail-loud)。
    infeasible_w = 0.0
    for code, w in list(tw.items()):
        px = prices.get(code)
        if px and px > 0 and w > 0 and min_lot * px > w * capital:
            infeasible_w += w
            skipped.append((code, "infeasible: 1手%.0f元 > 目标%.0f元" % (
                min_lot * px, w * capital)))
            tw[code] = 0.0
    all_codes = set(tw) | set(positions)
    for code in sorted(all_codes):
        px = prices.get(code)
        if not px or px <= 0:
            skipped.append((code, "no price/suspended"))
            continue
        cur_sh = positions.get(code, 0)
        tgt_w = tw.get(code, 0.0)
        tgt_sh = int(tgt_w * capital / px / min_lot) * min_lot
        diff = tgt_sh - cur_sh
        if tgt_w == 0.0 and cur_sh > 0:      # 清仓单不受碎单过滤(零股也卖)
            orders.append((code, "sell", cur_sh))
            continue
        if abs(diff) * px < min_trade_value:
            continue
        if diff > 0:
            pc = prev_closes.get(code)
            if pc and pc > 0 and px / pc - 1.0 >= gap_fuse:
                skipped.append((code, "gap %+.1f%% >= fuse" % ((px / pc - 1) * 100)))
                continue
            orders.append((code, "buy", diff))
        else:
            sell = min(-diff, cur_sh)
            if sell >= min_lot or tw.get(code, 0.0) == 0.0:
                orders.append((code, "sell", sell))
    orders.sort(key=lambda o: 0 if o[1] == "sell" else 1)
    return orders, skipped, infeasible_w


# ------------------------------------------------------------- QMT 接口层
def init(C):
    C.a500_done_month = ""          # 幂等标记: 本月已执行
    C.accid = ACCOUNT_ID
    print("[a500] init ok, csv=%s account=%s" % (CSV_PATH, C.accid))


def handlebar(C):
    if not C.is_last_bar():          # 只在最新K线动作(实盘bar)  # VERIFY
        return
    tt = C.get_bar_timetag(C.barpos)
    today_s = timetag_to_datetime(tt, "%Y-%m-%d")   # QMT 内置  # VERIFY
    today = datetime.strptime(today_s, "%Y-%m-%d").date()

    if C.a500_done_month == today_s[:7]:
        return

    if not os.path.exists(CSV_PATH):
        print("[a500][ALARM] 信号文件不存在: %s — 不交易" % CSV_PATH)
        return
    with open(CSV_PATH, "r") as f:
        try:
            meta, targets = parse_target_csv(f.read())
        except ValueError as e:
            print("[a500][ALARM] 信号文件损坏: %s — 不交易" % e)
            return
    ok, why = signal_fresh(meta, today)
    if not ok:
        if "未到执行日" not in why:
            print("[a500][ALARM] %s — 不交易" % why)
        return

    # ---- 账户与持仓 (miniQMT)  # VERIFY: 字段名对照本机 xtquant
    accts = get_trade_detail_data(C.accid, "stock", "account")
    if not accts:
        print("[a500][ALARM] 查不到账户 %s — 不交易" % C.accid)
        return
    total_asset = accts[0].m_dBalance          # 总资产  # VERIFY
    avail_cash = accts[0].m_dAvailable         # 可用资金  # VERIFY
    poss = get_trade_detail_data(C.accid, "stock", "position")
    positions, pos_value = {}, 0.0
    for p in poss:
        code = p.m_strInstrumentID + "." + p.m_strExchangeID   # VERIFY
        positions[code] = int(p.m_nCanUseVolume)               # 可卖(T+1)
        pos_value += float(p.m_dMarketValue)

    capital = min(MAX_CAPITAL, total_asset) * (1 - CASH_BUFFER)

    codes = sorted(set(c for c, _ in targets) | set(positions))
    prices, prev_closes = {}, {}
    md = C.get_market_data_ex(["open", "lastPrice", "lastClose"], codes,
                              period="1d", count=1)             # VERIFY
    for c in codes:
        try:
            row = md[c].iloc[-1]
            prices[c] = float(row.get("lastPrice") or row.get("open") or 0)
            prev_closes[c] = float(row.get("lastClose") or 0)
        except Exception:
            pass

    orders, skipped, infeasible_w = plan_orders(targets, positions, prices,
                                                prev_closes, capital)
    for code, reason in skipped:
        print("[a500] skip %s: %s" % (code, reason))
    if infeasible_w > 1 - MIN_FEASIBLE_WEIGHT:
        print("[a500][ALARM] %.0f%% 目标权重因资金不足买不起一手 (>%.0f%% 上限) — "
              "资金规模撑不起该策略, 拒绝交易; 忠实复制需约400万+, "
              "小资金请直接买 A500 ETF" % (infeasible_w * 100,
                                          (1 - MIN_FEASIBLE_WEIGHT) * 100))
        return
    if not orders:
        print("[a500] %s 无需调仓 (幂等)" % today_s)
        C.a500_done_month = today_s[:7]
        return

    n_buy = n_sell = 0
    for code, side, shares in orders:
        if side == "buy":
            est = shares * prices[code]
            if est > avail_cash:
                shares = int(avail_cash / prices[code] / MIN_LOT) * MIN_LOT
                if shares <= 0:
                    print("[a500] 现金不足, 跳过买 %s" % code)
                    continue
            avail_cash -= shares * prices[code]
            # opType 23=股票买入, orderType 1101=单股按股数, prType 5=最新价  # VERIFY
            passorder(23, 1101, C.accid, code, 5, -1, int(shares),
                      "a500_enhance", 1, "", C)
            n_buy += 1
        else:
            passorder(24, 1101, C.accid, code, 5, -1, int(shares),
                      "a500_enhance", 1, "", C)
            n_sell += 1
    print("[a500] %s 调仓完成: 卖 %d 买 %d (capital=%.0f)" % (
        today_s, n_sell, n_buy, capital))
    C.a500_done_month = today_s[:7]


# ---------------------------------------------------------------- 自测
def _selftest():
    csv_text = (
        "as_of,2026-07-15,execute_from,2026-07-16,top_k,50,band,2.0,n,3\n"
        "code,qmt_code,weight,composite\n"
        "600519,600519.SH,0.50,1.2\n"      # 超上限 → clip 10% 后重归一
        "000001,000001.SZ,0.30,0.9\n"
        "300750,300750.SZ,0.20,0.8\n")
    meta, targets = parse_target_csv(csv_text)
    w = dict(targets)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    # 三只都超 10% 上限 → 全 clip 到 0.10 → 重归一后各 1/3
    assert all(abs(x - 1 / 3) < 1e-9 for x in w.values())

    ok, _ = signal_fresh(meta, date(2026, 7, 16))
    assert ok
    ok, why = signal_fresh(meta, date(2026, 7, 30))
    assert not ok and "过期" in why
    ok, why = signal_fresh(meta, date(2026, 7, 15))
    assert not ok and "未到" in why

    targets2 = [("A.SH", 0.5), ("B.SZ", 0.5)]
    positions = {"B.SZ": 10000, "C.SH": 300}
    prices = {"A.SH": 10.0, "B.SZ": 20.0, "C.SH": 5.0}
    prev = {"A.SH": 9.5, "B.SZ": 20.0, "C.SH": 5.0}
    orders, skipped, inf_w = plan_orders(targets2, positions, prices, prev, 100000.0)
    od = {(c, s): sh for c, s, sh in orders}
    assert od[("A.SH", "buy")] == 5000          # 0.5*100000/10 → 5000股
    assert od[("B.SZ", "sell")] == 7500         # 10000 → 2500
    assert od[("C.SH", "sell")] == 300          # 清仓(不足一手也卖)
    assert orders[0][1] == "sell"               # 卖先于买
    assert inf_w == 0.0

    prev_gap = dict(prev)
    prev_gap["A.SH"] = 9.0
    prices_gap = dict(prices)
    prices_gap["A.SH"] = 10.0   # +11.1% 高开
    orders, skipped, _ = plan_orders(targets2, positions, prices_gap, prev_gap, 100000.0)
    assert ("A.SH", "buy", 5000) not in orders
    assert any("gap" in r for _, r in skipped)

    # 整手可行性: 一手 1500×100=15万 > 目标 0.5×20万=10万 → 剔除并记录缺口
    targets3 = [("HI.SH", 0.5), ("LO.SZ", 0.5)]
    prices3 = {"HI.SH": 1500.0, "LO.SZ": 10.0}
    prev3 = {"HI.SH": 1500.0, "LO.SZ": 10.0}
    orders, skipped, inf_w = plan_orders(targets3, {}, prices3, prev3, 200000.0)
    assert abs(inf_w - 0.5) < 1e-9
    assert any("infeasible" in r for _, r in skipped)
    assert not any(c == "HI.SH" for c, _, _ in orders)
    print("selftest OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("QMT strategy file; run with --selftest for logic checks")
