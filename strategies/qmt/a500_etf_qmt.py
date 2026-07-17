# coding: utf-8
# ---------------------------------------------------------------------------
# CSI A500 ETF 策略 —— QMT 执行器 (策略编辑器格式 init/handlebar)
#
# 定位(诚实立场护栏, 贯穿全文件):
#   本策略是「被动持有 A500 ETF 的贝塔 + 可选 MA 趋势护栏降回撤」, **不产生 alpha**。
#   研究依据 scripts/a500_etf_alpha.py: 在 000510 指数 2020-2026(含 2021顶/2022熊/
#   2024踩踏+修复/2025-26 AI牛) 上严验了 13 个单标的择时/波动管理/隔夜/日历候选,
#   全部无法 full&OOS 双净跑赢买入持有并过 DSR≥0.95 闸(DSR 全≈0, PBO 0.39)。
#   **唯一稳健、regime 无关的效应是 MA50 趋势滤波降回撤**: 把最大回撤从 -40% 砍到
#   ~-23%, 收益基本持平(超额夏普≈0, ≥10bps 成本超额翻负), 夏普 0.44→0.63。
#   => 打开趋势护栏 = 拿"少赚一点急涨反弹"换"熊市/踩踏少亏一大截", 是风控不是 alpha。
#      想吃满贝塔、不在乎回撤的小资金, 把 USE_TREND_FILTER 设 False 纯被动持有即可。
#   与 [[etf-grid-miner]] / A500 池内增强 (a500_enhance_qmt.py) 的立场一致:
#   指数层没有可实盘 alpha, 稳定成立的只有降回撤。
#
# 与 a500_enhance_qmt.py 的区别: 那个是 50 只成分股的池内增强执行器(读外部 CSV
#   目标权重); 本策略是**单标的 ETF**, 趋势信号在设备端从日 K 自算 => 无需 cron/
#   CSV 同步, 无信号超龄问题。
#
# 部署:
#   1) QMT 客户端(miniQMT)策略编辑器新建 python 策略, 粘贴本文件;
#   2) 改 ETF_CODE / ACCOUNT_ID / MAX_CAPITAL; 按需设 USE_TREND_FILTER / MA_WINDOW;
#   3) 周期选 日K, 运行方式先"模拟"跑通再"实盘"。
#
# 护栏(与仓内 RiskManager/实盘红线一致):
#   - 极端高开熔断: 开盘相对昨收 gap >= GAP_FUSE 当日不新买(彩票尾部, PR #121)
#   - 涨停不追买 / 跌停不砸卖(可选, 见 plan_order 注释)
#   - 总投入受 MAX_CAPITAL 限制, 留 CASH_BUFFER 现金缓冲
#   - 幂等: 按 目标-现状 差分下单, 同状态重复触发不重复买卖
#   - 碎单过滤: 小于 MIN_TRADE_VALUE 的调整忽略
#
# 除 init/handlebar 外的纯逻辑函数不依赖 QMT, 可直接自测:
#   python strategies/qmt/a500_etf_qmt.py --selftest
# 标注 # VERIFY 的行是 QMT API 细节, 上真机时对照本机 xtquant 版本核对一次。
# ---------------------------------------------------------------------------

# ----------------------------------------------------------------- 用户配置
ETF_CODE = "512050.SH"       # A500 ETF(如 512050.SH 华夏/159339.SZ 易方达等); 按券商可交易标的改
ACCOUNT_ID = "REPLACE_ME"    # miniQMT 资金账号
MAX_CAPITAL = 200_000.0      # 本策略最大投入(元)
CASH_BUFFER = 0.01           # 留 1% 现金缓冲防超买
USE_TREND_FILTER = True      # True=MA 趋势护栏(降回撤); False=纯被动满仓持有(吃满贝塔)
MA_WINDOW = 50               # 趋势均线窗口(研究里 50 日回撤/夏普最优且最稳健)
GAP_FUSE = 0.10              # 极端高开熔断阈值(+10%)
MIN_LOT = 100                # A股/ETF 一手
MIN_TRADE_VALUE = 2_000.0    # 小于该金额的调整忽略(防碎单)


# ------------------------------------------------------------ 纯逻辑(可自测)
def target_exposure(closes, use_trend=USE_TREND_FILTER, ma_window=MA_WINDOW):
    """根据历史收盘价决定目标敞口 ∈ {0.0, 1.0}。

    closes: 到"昨日"为止的收盘价序列(升序, 最后一个=最近已收盘日)。
    - use_trend=False: 恒满仓 1.0(纯被动)。
    - use_trend=True : 昨收 > MA(ma_window) 则 1.0(在场), 否则 0.0(空仓避险)。
      历史不足 ma_window 根时数据不够, 保守空仓(与回测 shift(1)+早期无仓一致)。
    只用已收盘数据 => 无前视; 当日按此敞口在次日/当日开盘调仓。
    """
    if not use_trend:
        return 1.0
    if len(closes) < ma_window:
        return 0.0
    ma = sum(closes[-ma_window:]) / ma_window
    return 1.0 if closes[-1] > ma else 0.0


def plan_order(exposure, position, price, prev_close, capital,
               gap_fuse=GAP_FUSE, min_lot=MIN_LOT, min_trade_value=MIN_TRADE_VALUE):
    """单标的目标敞口 vs 当前持仓 → (side, shares) 或 (None, 0)。

    exposure:  目标敞口 0.0/1.0
    position:  当前可卖股数(T+1)
    price:     现价(停牌=None/<=0 时不动)
    prev_close:昨收(算 gap 熔断)
    capital:   本策略计划总市值上限(含现有持仓市值)
    返回 side ∈ {'buy','sell',None}。清仓(exposure=0)不受碎单过滤; 高开熔断时不新买。
    """
    if not price or price <= 0:
        return None, 0, "no price/suspended"
    tgt_sh = int(exposure * capital / price / min_lot) * min_lot
    diff = tgt_sh - position
    if diff > 0:
        if abs(diff) * price < min_trade_value:
            return None, 0, "below min_trade_value"
        if prev_close and prev_close > 0 and price / prev_close - 1.0 >= gap_fuse:
            return None, 0, "gap %+.1f%% >= fuse" % ((price / prev_close - 1) * 100)
        return "buy", diff, ""
    if diff < 0:
        sell = min(-diff, position)
        # 清仓单(目标 0)不受碎单过滤; 减仓单需 >= 一手且金额达标
        if exposure == 0.0 and position > 0:
            return "sell", position, ""
        if sell >= min_lot and abs(diff) * price >= min_trade_value:
            return "sell", sell, ""
    return None, 0, "no change (idempotent)"


# ------------------------------------------------------------- QMT 接口层
def init(C):
    C.a500etf_last_day = ""          # 幂等标记: 本交易日已执行
    C.accid = ACCOUNT_ID
    print("[a500etf] init ok, code=%s account=%s trend=%s(MA%d)" % (
        ETF_CODE, C.accid, USE_TREND_FILTER, MA_WINDOW))


def handlebar(C):
    if not C.is_last_bar():          # 只在最新K线动作(实盘bar)  # VERIFY
        return
    tt = C.get_bar_timetag(C.barpos)
    today_s = timetag_to_datetime(tt, "%Y-%m-%d")   # QMT 内置  # VERIFY
    if C.a500etf_last_day == today_s:
        return

    # ---- 历史收盘价(算 MA, 需 >= MA_WINDOW+1 根; 取 lastClose 序列)  # VERIFY
    need = MA_WINDOW + 5 if USE_TREND_FILTER else 2
    hist = C.get_market_data_ex(["close"], [ETF_CODE], period="1d", count=need)
    try:
        closes = [float(x) for x in hist[ETF_CODE]["close"].tolist() if x and x > 0]
    except Exception as e:
        print("[a500etf][ALARM] 取历史收盘失败: %s — 不交易" % e)
        return
    # 最后一根若是当日未收盘的实时 bar, MA 用「已收盘」序列: 去掉最后一根做 MA,
    # 但敞口判定用"昨收 vs MA(截至昨收)"。这里 closes[-1] 视作昨收(日K在开盘时
    # 最新一根通常是昨日已收)。上真机核对 count 语义一次。  # VERIFY
    exposure = target_exposure(closes, USE_TREND_FILTER, MA_WINDOW)

    # ---- 账户与持仓 (miniQMT)  # VERIFY: 字段名对照本机 xtquant
    accts = get_trade_detail_data(C.accid, "stock", "account")
    if not accts:
        print("[a500etf][ALARM] 查不到账户 %s — 不交易" % C.accid)
        return
    total_asset = accts[0].m_dBalance          # 总资产  # VERIFY
    avail_cash = accts[0].m_dAvailable         # 可用资金  # VERIFY
    poss = get_trade_detail_data(C.accid, "stock", "position")
    position = 0
    for p in poss:
        if p.m_strInstrumentID + "." + p.m_strExchangeID == ETF_CODE:   # VERIFY
            position = int(p.m_nCanUseVolume)                            # 可卖(T+1)

    capital = min(MAX_CAPITAL, total_asset) * (1 - CASH_BUFFER)

    md = C.get_market_data_ex(["open", "lastPrice", "lastClose"], [ETF_CODE],
                              period="1d", count=1)             # VERIFY
    try:
        row = md[ETF_CODE].iloc[-1]
        price = float(row.get("lastPrice") or row.get("open") or 0)
        prev_close = float(row.get("lastClose") or 0)
    except Exception:
        print("[a500etf][ALARM] 取现价失败 — 不交易")
        return

    side, shares, why = plan_order(exposure, position, price, prev_close, capital)
    if side is None:
        if why not in ("no change (idempotent)",):
            print("[a500etf] %s skip: %s (敞口目标=%.0f)" % (today_s, why, exposure))
        C.a500etf_last_day = today_s
        return

    if side == "buy":
        est = shares * price
        if est > avail_cash:
            shares = int(avail_cash / price / MIN_LOT) * MIN_LOT
            if shares <= 0:
                print("[a500etf] 现金不足, 跳过买入")
                C.a500etf_last_day = today_s
                return
        # opType 23=买入, orderType 1101=按股数, prType 5=最新价  # VERIFY
        passorder(23, 1101, C.accid, ETF_CODE, 5, -1, int(shares), "a500_etf", 1, "", C)
        print("[a500etf] %s 买入 %d 股 (敞口=1, MA%d过滤=%s, capital=%.0f)" % (
            today_s, shares, MA_WINDOW, USE_TREND_FILTER, capital))
    else:
        passorder(24, 1101, C.accid, ETF_CODE, 5, -1, int(shares), "a500_etf", 1, "", C)
        print("[a500etf] %s 卖出 %d 股 (敞口目标=%.0f, 趋势护栏避险)" % (
            today_s, shares, exposure))
    C.a500etf_last_day = today_s


# ---------------------------------------------------------------- 自测
def _selftest():
    # target_exposure: 纯被动恒 1
    assert target_exposure([1, 2, 3], use_trend=False) == 1.0
    # 趋势: 数据不足 → 空仓
    assert target_exposure([1, 2, 3], use_trend=True, ma_window=50) == 0.0
    # 昨收 > MA → 在场; 昨收 < MA → 空仓
    up = list(range(1, 61))                       # 单调涨, 末值 > MA50
    assert target_exposure(up, use_trend=True, ma_window=50) == 1.0
    down = list(range(60, 0, -1))                 # 单调跌, 末值 < MA50
    assert target_exposure(down, use_trend=True, ma_window=50) == 0.0

    # plan_order: 空仓 → 满仓买入 (int(100000/10/100)*100=10000)
    side, sh, _ = plan_order(1.0, 0, 10.0, 10.0, 100_000.0)
    assert side == "buy" and sh == 10000
    # 已满仓 → 幂等不动
    side, sh, why = plan_order(1.0, 10000, 10.0, 10.0, 100_000.0)
    assert side is None and "idempotent" in why
    # 目标空仓 → 全清(不受碎单过滤, 零股也卖)
    side, sh, _ = plan_order(0.0, 50, 10.0, 10.0, 100_000.0)
    assert side == "sell" and sh == 50
    # 高开熔断: +11% gap 不新买
    side, sh, why = plan_order(1.0, 0, 11.1, 10.0, 100_000.0)
    assert side is None and "gap" in why
    # 停牌无价 → 不动
    side, sh, why = plan_order(1.0, 0, 0, 10.0, 100_000.0)
    assert side is None and "suspended" in why
    # 减仓: 持 20000 目标 10000 → 卖 10000
    side, sh, _ = plan_order(1.0, 20000, 10.0, 10.0, 100_000.0)
    assert side == "sell" and sh == 10000
    print("selftest OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("QMT strategy file; run with --selftest for logic checks")
