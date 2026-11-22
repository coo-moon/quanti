"""前向模拟盘 runner — 过闸新策略挂一张独立纸面账户,日频跟净值/超额/回撤。

回测过闸 ≠ 前瞻有效。每个过闸策略(#197 sse_enhance 是第一个)都要在一张
**独立**模拟账户里跑足够长的真实时间,用只往前写的净值序列去证实/证伪回测
结论。本脚本就是那张账的记账员:

    init    建账户库 + 把「策略名 / 初始资金」登记进该账户 app_config,
            并立刻跑一次 tick 建仓
    tick    幂等推进:先让昨日挂单按次日开盘成交,再按策略目标权重 diff
            出新挂单。**可日内多次调用** —— 同一信号日只挂一批(决策日志查重)
    report  把账户现状以 JSON 打到 stdout(净值/超额/回撤/换手/挂单/告警)

三条纪律(与 sse_enhance 的 docstring 对齐,违反即失真):
  1. 复制类策略的 signal.strength = 目标组合权重,**不走** agent 的逐股信号
     路径,也不由本脚本二次择时:信号日取行情库最新全市交易日,目标权重取
     策略自身 on_bar 结果,月频节奏来自策略内部;
  2. 不带任何 per-stock 离场 overlay:tick 从不调 check_exits/止损止盈,
     并显式关掉 protections(它们在最开始就拒单,同样会破坏复制);
  3. 账户按 QUANTI_ACCOUNT 分文件(data/{account}.db),market.db 共享,
     绝不与交互式 paper / 实盘 live 账户混用(名字撞上即 fail-loud)。

撮合 100% 复用 PaperBroker:挂单 T+1 次日开盘成交、涨跌停/一字板闸门、
整手、A股费用与红利税、滑点上限照旧。本脚本不改 engine/paper_broker 任何
语义,只做「组合权重 → 买卖挂单」这一层适配。

时段外安全:tick/report 都以「行情库里最新的全市交易日」为信号日并把它标在
data_asof 上,数据滞后时照跑(滞后本身进 alerts)。

用法(仓库根):
    /opt/data/quanti/.venv/bin/python scripts/paper_forward.py init \
        --account fwd_sse --strategy sse_enhance --cash 500000
    /opt/data/quanti/.venv/bin/python scripts/paper_forward.py tick --account fwd_sse
    /opt/data/quanti/.venv/bin/python scripts/paper_forward.py report --account fwd_sse

外部调度器每日收盘后调一次 tick 即可;report 供周报/看板消费。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("paper_forward")

# tick 决策查重用的决策日志 kind:同一「信号日」只允许挂一批单
TICK_KIND = "forward_tick"
# 目标权重向量的归一容差(策略按构造给出 Σw=1;超出即说明它不是组合权重类)
WEIGHT_SUM_TOL = 0.02
# 复制盘一篮子可能要挂几百只(全样本),逐股风控的日单数上限在这里必须放开
FORWARD_MAX_DAILY_ORDERS = 5000
# 找信号日收盘价的回看窗口(自然日):容忍长假 + 停牌几天的空档
PRICE_LOOKBACK_DAYS = 30
# 净值/基准的滞后告警阈值(自然日)
STALE_ALERT_DAYS = 5
# report 里 pending_orders.sample 的截断条数
PENDING_SAMPLE = 20
# 前向账户名黑名单:这两个是「人的账」,不能被机器挂账覆盖
INTERACTIVE_ACCOUNTS = ("paper", "live")

FORWARD_SIGNAL_BAR_CODE = "__forward_signal_bar__"
"""占位 bar 的 code:复制类策略的 on_bar 只读 bar.date(按月取快照),不读代码。
每个 tick 都用全新策略实例,于是权重向量恒等于「本月用上月末快照」的目标,
策略自身的月频节奏被原样复现,脚本不做第二次择时。"""


class ForwardError(RuntimeError):
    """配置/校验类硬错误:前向盘宁可不出账,也不能出错账。"""


# ---------------------------------------------------------------- 上下文

def _default_data_dir() -> str:
    return os.environ.get("QUANTI_DATA_DIR") or str(ROOT / "data")


class Ctx:
    """一次命令的运行时上下文(账户库 + 共享行情库 + 策略目录)。"""

    def __init__(self, account: str, data_dir: Path, market_db: Path,
                 strategies_dir: str) -> None:
        from quanti.data.database import Database
        from quanti.data.provider import DataProvider

        self.account = account
        self.data_dir = data_dir
        self.market_db = market_db
        self.strategies_dir = strategies_dir
        self.account_db_path = data_dir / f"{account}.db"
        if not market_db.exists():
            raise ForwardError(
                f"共享行情库不存在:{market_db}\n"
                "前向模拟盘需要 daily_quotes/daily_basic;先跑 quanti sync "
                "或用 --market-db 指到实际路径。")
        self.db = Database(str(self.account_db_path),
                           market_db_path=str(market_db))
        self.db.initialize()
        self.provider = DataProvider(self.db)


def resolve_account(args) -> str:
    acct = (args.account or os.environ.get("QUANTI_ACCOUNT", "")).strip()
    if not acct:
        raise ForwardError("未指定账户:给 --account 或设 QUANTI_ACCOUNT。")
    if acct in INTERACTIVE_ACCOUNTS:
        raise ForwardError(
            f"拒绝使用前向账户名 {acct!r}:那是{'实盘' if acct == 'live' else '交互式模拟'}"
            "账户,前向挂账必须用独立账户名(约定 fwd_<策略>,如 fwd_sse)。")
    return acct


def open_ctx(args) -> Ctx:
    account = resolve_account(args)
    data_dir = Path(args.data_dir or _default_data_dir()).expanduser()
    market_db = Path(args.market_db or os.environ.get("QUANTI_MARKET_DB")
                      or (data_dir / "market.db")).expanduser()
    return Ctx(account=account, data_dir=data_dir, market_db=market_db,
               strategies_dir=args.strategies_dir)


# ------------------------------------------------------ 策略校验与加载

def _load_by_name(strategies_dir: str, name: str):
    """在 strategies/ 顶层按 name 找策略实例;找不到就 fail-loud 列出可选项。"""
    from quanti.strategy.loader import StrategyLoader

    insts = StrategyLoader().load_directory(strategies_dir)
    for inst in insts:
        if getattr(inst, "name", "") == name:
            return inst
    raise ForwardError(
        f"strategies/ 里没有名为 {name!r} 的策略(目录 {strategies_dir})。"
        f"可选:{sorted(getattr(i, 'name', '') for i in insts)}")


def _assert_portfolio_weight(strat) -> None:
    """确认这是「strength = 组合权重」类策略,而不是逐股技术信号策略。

    判据(与 sse_enhance / dividend_tr_enhance 的契约一字不差):
      - param_space == {}:复制规则非优化产物;有可调参的应先过 walk-forward,
        前向 runner 不承担选参(选了就是拿前向账户做过拟合的尾巴);
      - init() 后自带 preferred_sizer = FixedSizer(max_pct=1.0):strength
        才能被当成整组合权重直通(否则 broker 会按 cash×strength 逐股下单);
      - selectable=False:逐股 selector 执行假设与月频数百持仓不兼容。
    """
    problems = []
    if getattr(strat, "param_space", None):
        problems.append(f"param_space 非空 {sorted(strat.param_space)}")
    sizer = getattr(strat, "preferred_sizer", None)
    if sizer is None:
        problems.append("没有 preferred_sizer(FixedSizer(max_pct=1.0) 缺失,"
                        "strength 不会被当成组合权重)")
    elif type(sizer).__name__ != "FixedSizer" or float(
            getattr(sizer, "_max_pct", 0.0)) < 1.0:
        problems.append(f"preferred_sizer={type(sizer).__name__}"
                        f"(max_pct={getattr(sizer, '_max_pct', None)}) 不是"
                        " FixedSizer(max_pct=1.0)")
    if getattr(strat, "selectable", True):
        problems.append("selectable=True(逐股 selector 策略,不走本 runner)")
    if problems:
        raise ForwardError(
            f"{getattr(strat, 'name', '?')} 不是组合权重类策略,拒绝挂前向账:\n  - "
            + "\n  - ".join(problems))


def build_strategy(ctx: Ctx, cfg: dict):
    """按登记表建一个**全新**策略实例(每 tick 一个,跨进程无状态)。"""
    inst = _load_by_name(ctx.strategies_dir, cfg["strategy"])
    cls = type(inst)
    strat = cls()
    market_cfg = dict(cfg.get("strategy_config") or {})
    strat.init({"market_db_path": str(ctx.market_db), **market_cfg})
    _assert_portfolio_weight(strat)
    return strat


def target_weights(strat, asof: date) -> dict[str, float]:
    """策略在信号日 asof 的目标权重 {code: weight}(Σw≈1)。"""
    from quanti.models import BarData, Direction

    bar = BarData(code=FORWARD_SIGNAL_BAR_CODE, date=asof, open=1.0,
                  high=1.0, low=1.0, close=1.0, volume=0.0, amount=0.0)
    weights: dict[str, float] = {}
    for sig in strat.on_bar(bar):
        if sig.direction == Direction.BUY:
            weights[sig.stock_code] = float(sig.strength)
    if not weights:
        return {}
    total = sum(weights.values())
    bad = [c for c, w in weights.items() if not 0.0 < w <= 1.0]
    if bad:
        raise ForwardError(
            f"{getattr(strat, 'name', '?')} 的 strength 不是合法权重"
            f"(如 {bad[:3]}),本 runner 只接受「strength=组合权重」类策略。")
    if abs(total - 1.0) > WEIGHT_SUM_TOL:
        raise ForwardError(
            f"{getattr(strat, 'name', '?')} 在 {asof} 给出的权重和为 {total:.4f}"
            f"(应≈1.0):它不是归一的组合权重向量,或参考数据缺失。")
    return weights


def bench_code_for(ctx: Ctx, cfg: dict, name: str) -> str:
    """基准代码:app_config 覆盖 > 策略声明(cls.bench_code)> 报错。"""
    over = str(cfg.get("bench_code") or "").strip()
    if over:
        return over
    declared = str(getattr(_load_by_name(ctx.strategies_dir, name),
                           "bench_code", "") or "").strip()
    if declared:
        return declared
    raise ForwardError(
        f"{name} 未声明基准(类属性 bench_code),且 app_config 里没有 "
        "bench_code 覆盖 —— 报告需要基准才能算超额;用 init --bench-code 指定。")


# ---------------------------------------------------------------- 撮合

def make_broker(ctx: Ctx, initial_cash: float, sizer):
    """建前向账户的 PaperBroker(pending 模式:T+1 次日开盘成交)。

    风控口径为「复制盘专用」:单票/行业上限放开(全样本市值加权本身就是
    分散),逐股离场与保护层整体旁路 —— 成分卖掉不回补就是跟踪误差。
    组合级 -30% 熔断阈值保留在配置里,但 tick **不调** enforce_portfolio_stop
    (清仓等于毁掉这轮前向验证),改由 report 的 alerts 通知人。
    """
    from quanti.execution.paper_broker import PaperBroker
    from quanti.risk.manager import RiskConfig
    from quanti.risk.protections import ProtectionConfig

    # PaperBroker._sync_risk_config 会在每次推进挂单时用库里的 risk_config
    # 行整体覆盖上面的口径(该行没有 max_daily_trades 列 → 回落 20 单/日,
    # 一篮子建仓会被静默砍剩 20 只)。前向账户不接受这种覆盖,直接 fail-loud。
    if ctx.db.get_risk_config():
        raise ForwardError(
            f"账户库 {ctx.account_db_path} 里存在 risk_config 行:它会覆盖"
            "复制盘所需的宽松风控(且无法表达日单数上限)。前向账户请删除该行"
            "或换账户名 —— 风控调参属于交互 paper 账户的事。")
    broker = PaperBroker(
        db=ctx.db, provider=ctx.provider, initial_cash=initial_cash,
        fill_mode="pending",
        risk_config=RiskConfig(
            max_position_pct=1.0, max_industry_pct=1.0,
            max_daily_trades=FORWARD_MAX_DAILY_ORDERS,
            strategy_exit_enabled=False, atr_stop_k=0.0,
            take_profit_activate_pct=0.0),
        protection_config=ProtectionConfig(enabled=False),
        strategies_dir=ctx.strategies_dir,
        # 前向盘只做收盘口径决策:不接实时行情源(腾讯/xtdata),因此
        # snapshot 恒为收盘价、永不走盘中 live-mirror 分支。
        realtime_quote_fn=None,
    )
    broker.set_sizer(sizer)
    return broker


def mark_nav(ctx: Ctx, broker, asof: date) -> dict:
    """把账户 mark 到最新收盘,并保证净值序列按**行情数据日**对齐落库。

    PaperBroker.snapshot_portfolio 在空仓时(没有任何价格日期可锚)会把快照
    记在墙钟日:行情滞后时那就变成一行「未来」净值,污染只增序列。这里补
    一行 asof 口径的快照(有持仓时 snap 日期本就等于 asof → 同值幂等重写)。
    只调既有 DB 写接口,不动 broker 的撮合/定价语义。
    """
    snap = broker.snapshot_portfolio()
    if str(snap.get("snapshot_date") or "") != asof.isoformat():
        ctx.db.save_portfolio_snapshot(
            asof, float(snap["cash"]), float(snap["market_value"]),
            float(snap["total_value"]))
    return snap


def signal_date(ctx: Ctx) -> date:
    """信号日 = 行情库最新的**全市**交易日(不是裸 MAX(date),避免个别
    代码手工补数把整个库标新鲜)。"""
    d = ctx.db.get_market_latest_quote_date()
    if d is None:
        raise ForwardError("行情库 daily_quotes 为空:先同步行情再挂前向账。")
    return d


def _close_map(ctx: Ctx, asof: date) -> dict[str, float]:
    """{code: 最近一根 hfq 收盘}(asof 前 PRICE_LOOKBACK_DAYS 窗口内)。

    整手筛查要的是「策略/broker 未来实际用来定价的那个价」:provider 的
    hfq 口径 = raw close x adj_factor,故这里同式。一次批量查询而不是逐码
    往返(全样本 2000+ 码时,逐码查是分钟级、批量是毫秒级)。窗口内无 bar
    的代码(长期停牌/退市)不进表 → 按「无价」处理,交给引擎的 TTL 弃单。
    """
    start = (asof - timedelta(days=PRICE_LOOKBACK_DAYS)).isoformat()
    rows = ctx.db.conn.execute(
        "SELECT code, date, close * adj_factor FROM daily_quotes"
        " WHERE date>=? AND date<=? ORDER BY date",
        (start, asof.isoformat()),
    ).fetchall()
    out: dict[str, float] = {}
    for code, _d, close in rows:  # 按日期升序,后写覆盖前写 = 每码最后一根
        if close is not None:
            out[code] = float(close)
    return out


# ------------------------------------------------------- 幂等查重

def already_ticked(db, asof: date) -> bool:
    """本信号日是否已经挂过一批单(决策日志查重,可日多次调)。"""
    stamp = asof.isoformat()
    for row in db.list_decisions(limit=500, kind=TICK_KIND):
        if str((row.get("details") or {}).get("signal_date")) == stamp:
            return True
    return False


# ---------------------------------------------------------------- init

def cmd_init(ctx: Ctx, args) -> dict:
    db = ctx.db
    registered = db.get_forward_config()
    cash = float(args.cash)
    if cash <= 0:
        raise ForwardError("--cash 必须为正数。")
    state = db.get_portfolio_state()
    if registered and not args.force:
        raise ForwardError(
            f"账户 {ctx.account} 已登记策略 {registered.get('strategy')!r}"
            "(改登记请换账户名,或加 --force)。")
    if state and not args.force:
        if state.get("initial_cash") and abs(
                float(state["initial_cash"]) - cash) > 1e-6:
            raise ForwardError(
                f"账户已有净值起点 initial_cash={state['initial_cash']:,.0f},"
                f"与 --cash {cash:,.0f} 不一致。改资金请换账户名(账本不可篡改),"
                "或确知要仅重登策略时加 --force。")

    strat_name = args.strategy
    # 先按策略声明建一次实例做「接口 + 参考数据」双校验(不满足即 fail-loud,
    # 绝不允许挂一张建仓为空的账)。
    probe_cfg = {"strategy": strat_name,
                 "strategy_config": args.strategy_config_obj or {}}
    probe = build_strategy(ctx, probe_cfg)
    asof = signal_date(ctx)
    w = target_weights(probe, asof)
    if not w:
        raise ForwardError(
            f"{strat_name} 在信号日 {asof} 给不出目标权重:参考数据缺失"
            "(多半是上月末 daily_basic 市值快照还没回填)。先补数据再 init。")
    bench = args.bench_code or bench_code_for(ctx, {}, strat_name)
    logger.info("基准锚 %s(超额以此为基准计算)", bench)

    # 已有账本时净值起点以账本为准:重登策略绝不篡改历史 initial_cash
    if state and state.get("initial_cash"):
        logger.info("沿用既有账本 initial_cash=%.0f",
                    float(state["initial_cash"]))
        cash = float(state["initial_cash"])

    # 登记表由 init 的命令行参数整体重写(不传 = 清空),改登记时不会把上一个
    # 账户的 bench_code / 策略参数粘过来。起点 = 信号日而不是墙钟日:账本按
    # 行情数据日起算,数据滞后时才不会第一天就凭空多出几天账龄。
    saved = db.set_forward_config(
        strategy=strat_name, initial_cash=cash,
        bench_code=args.bench_code, strategy_config=args.strategy_config_obj or {},
        started_at=asof.isoformat(), account=ctx.account)
    logger.info("已登记前向账户 %s:%s cash=%.0f started_at=%s",
                ctx.account, strat_name, cash, saved.get("started_at"))

    # 立刻跑一次 tick 建仓
    out = run_tick(ctx, saved)
    out["action"] = "init"
    return out


# ---------------------------------------------------------------- tick

def run_tick(ctx: Ctx, cfg: dict) -> dict:
    from quanti.backtest.commission import AShareCommission
    from quanti.models import Direction, Signal

    db = ctx.db
    strat_name = str(cfg["strategy"])
    initial_cash = float(cfg.get("initial_cash") or 0.0)
    state = db.ensure_portfolio(initial_cash)
    # 账本里的 initial_cash 才是净值起点(重登不改历史)
    initial_cash = float(state["initial_cash"] or initial_cash or 1.0)

    strat = build_strategy(ctx, cfg)
    broker = make_broker(ctx, initial_cash, strat.preferred_sizer)

    asof = signal_date(ctx)
    fills = broker.try_fill_pending_orders()
    snap = mark_nav(ctx, broker, asof)
    total_value = float(snap["total_value"] or initial_cash)

    out: dict = {
        "action": "tick", "account": ctx.account, "strategy": strat_name,
        "signal_date": asof.isoformat(), "data_asof": asof.isoformat(),
        "idempotent_skip": False,
        "fills": {"scanned": fills.scanned, "filled": fills.filled,
                  "rejected": fills.rejected, "expired": fills.expired,
                  "still_pending": fills.still_pending},
        "queued": {"buy": 0, "sell": 0}, "rejected_submit": 0,
        "skipped_unaffordable": 0, "dropped_weight": 0.0,
        "target_n": 0, "positions": len(snap["positions"]),
        "cash": round(float(snap["cash"]), 2),
        "total_value": round(total_value, 2),
        "alerts": [],
    }

    if already_ticked(db, asof):
        out["idempotent_skip"] = True
        logger.info("信号日 %s 已挂过单,幂等跳过建单(仍推进成交/净值)", asof)
    else:
        weights = target_weights(strat, asof)
        out["target_n"] = len(weights)
        if not weights:
            out["alerts"].append(
                f"策略 {strat_name} 在 {asof} 未给目标权重(上月末参考快照缺失),"
                "本次 tick 不建单。")
        else:
            held = {p["code"] for p in db.list_positions()}
            pending = {(o["code"], o["direction"])
                       for o in db.list_orders(limit=3000, status="pending")}
            closes = _close_map(ctx, asof)
            commission = AShareCommission()
            sells, buys, dropped = [], [], []
            for code in sorted(held - set(weights)):
                if (code, "sell") in pending:
                    continue
                sells.append(Signal(
                    stock_code=code, direction=Direction.SELL, strength=1.0,
                    reason="移出复制样本(前向调仓)", entry_strategy=strat_name))
            for code, w in sorted(weights.items(), key=lambda kv: -kv[1]):
                if code in held or (code, "buy") in pending:
                    continue
                px = closes.get(code)
                if px is not None:
                    # 整手约束:目标金额连一手都买不起 → 不挂 doomed 单
                    # (策略自己的 min_weight 截断才是正解,报告里如实记账)。
                    lot_cost = px * 100 * 1.005 + commission.calculate(
                        px * 100, 100, Direction.BUY)
                    if w * total_value < lot_cost:
                        dropped.append(code)
                        continue
                buys.append(Signal(
                    stock_code=code, direction=Direction.BUY,
                    strength=max(min(w, 1.0), 1e-6),
                    reason=f"前向复制建仓 w={w:.4%}",
                    entry_strategy=strat_name))
            res = broker.execute_signals(sells + buys, strategy_name=strat_name)
            out["queued"] = {"buy": len(buys), "sell": len(sells)}
            out["rejected_submit"] = res.rejected
            out["skipped_unaffordable"] = len(dropped)
            out["dropped_weight"] = round(sum(weights[c] for c in dropped), 6)
            if res.rejected:
                out["alerts"].append(
                    f"{res.rejected} 笔挂单在排队时被拒(风控/资金/无持仓):"
                    f"{res.reasons[:3]}")
            snap = mark_nav(ctx, broker, asof)
            out["total_value"] = round(float(snap["total_value"]), 2)
            out["cash"] = round(float(snap["cash"]), 2)

    db.log_decision(
        TICK_KIND,
        f"前向 tick {ctx.account}/{strat_name} @ {asof}: "
        f"成交 {out['fills']['filled']} 挂单 买{out['queued']['buy']}"
        f"/卖{out['queued']['sell']} 跳过整手买不起 {out['skipped_unaffordable']}",
        details={k: v for k, v in out.items() if k not in ("alerts",)},
    )
    return out


def cmd_tick(ctx: Ctx, args) -> dict:
    cfg = _require_registered(ctx)
    return run_tick(ctx, cfg)


def _require_registered(ctx: Ctx) -> dict:
    cfg = ctx.db.get_forward_config()
    if not cfg.get("strategy"):
        raise ForwardError(
            f"账户 {ctx.account} 未登记前向策略:先跑 "
            f"scripts/paper_forward.py init --account {ctx.account} "
            "--strategy <name> --cash <元>。")
    if not cfg.get("initial_cash"):
        raise ForwardError("前向登记缺少 initial_cash,净值无锚点。")
    return cfg


# ---------------------------------------------------------------- report

def _as_date(v) -> date | None:
    """宽容解析 ISO 日期串(注册表里的 started_at 可能被手改坏)。"""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _bench_frame(ctx: Ctx, bench_code: str, refresh: bool) -> tuple:
    """基准收盘价序列(index=ISO 日期)。沿用项目既有约定:
    tushare index_daily → data/idx_<code>.csv 缓存(sse_enhance 系列脚本同源)。

    返回 (series, warn)。refresh=True 才打网络;默认纯本地读,调度器天天
    跑 report 也不会碰外网。
    """
    import pandas as pd

    path = ctx.data_dir / f"idx_{bench_code.replace('.', '_')}.csv"
    if refresh:
        try:
            _refresh_bench_csv(path, bench_code, ctx)
        except Exception as exc:  # noqa: BLE001 - 拉不到就用本地缓存
            logger.warning("基准 %s 刷新失败,改用本地缓存:%s", bench_code, exc)
    if not path.exists():
        return pd.Series(dtype=float), (
            f"基准 {bench_code} 本地序列缺失({path.name});跑 "
            "report --refresh-bench 拉一次。")
    df = pd.read_csv(path, dtype={"trade_date": str}).dropna(
        subset=["trade_date", "close"]).sort_values("trade_date")
    iso = (df.trade_date.str[:4] + "-" + df.trade_date.str[4:6]
           + "-" + df.trade_date.str[6:])
    return pd.Series(df.close.astype(float).values, index=iso), None


def _refresh_bench_csv(path: Path, bench_code: str, ctx: Ctx) -> None:
    import tushare as ts

    tok = os.environ.get("TUSHARE_TOKEN") or _token_from_db(ctx)
    if not tok:
        raise RuntimeError("无 tushare token(env TUSHARE_TOKEN 或配置库 app_config)")
    df = ts.pro_api(tok).index_daily(
        ts_code=bench_code, start_date="20150801",
        end_date=date.today().strftime("%Y%m%d"))
    if df is None or df.empty:
        raise RuntimeError(f"tushare index_daily {bench_code} 返回空")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    logger.info("基准 %s 已刷新 → %s(%d 行,截至 %s)", bench_code, path.name,
                len(df), str(df.trade_date.max()))


def _token_from_db(ctx: Ctx) -> str:
    """token 兜底:同目录里已有的配置库(paper.db / quanti.db)的 app_config。"""
    for name in ("paper.db", "quanti.db"):
        path = ctx.data_dir / name
        if not path.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            row = con.execute(
                "select data_source_token from app_config").fetchone()
        except sqlite3.Error:
            row = None
        finally:
            con.close()
        if row and row[0]:
            return str(row[0])
    return ""


def _close_on_or_before(series, d: date) -> tuple[date, float] | None:
    """series 上 <= d 的最后一个 (日期, 收盘)。"""
    if series is None or len(series) == 0:
        return None
    pos = series.index.searchsorted(d.isoformat(), side="right")
    if pos <= 0:
        return None
    return date.fromisoformat(str(series.index[pos - 1])), \
        float(series.iloc[pos - 1])


def _max_drawdown(values: list[float]) -> float:
    """峰谷最大回撤(负数)。不足 2 个点 → 0.0。"""
    peak = float("-inf")
    dd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            dd = min(dd, v / peak - 1.0)
    return dd


def last_build_marker(db) -> dict:
    """最近一次**真正建单**的 tick 明细(跳过幂等空转的那些)。

    幂等跳过的 tick 同样落标记 —— 那正是「同一信号日不双挂单」的依据 ——
    但它的 target_n / skipped_unaffordable 恒为 0,拿它做告警会误报。"""
    for row in db.list_decisions(limit=500, kind=TICK_KIND):
        d = row.get("details") or {}
        if not d.get("idempotent_skip"):
            return d
    return {}


def cmd_report(ctx: Ctx, args) -> dict:
    db = ctx.db
    cfg = _require_registered(ctx)
    strat_name = str(cfg["strategy"])
    asof = signal_date(ctx)
    today = date.today()

    # 与 tick 同一套幂等写入:把持仓 mark 到最新收盘并落一行净值快照 ——
    # 逐日净值用项目已有的 portfolio_snapshots 表(不另造),这样只跑 report
    # 不跑 tick 的日子净值序列也不断档。
    strat = build_strategy(ctx, cfg)
    state = db.ensure_portfolio(float(cfg["initial_cash"]))
    broker = make_broker(ctx, float(state["initial_cash"]),
                         strat.preferred_sizer)
    snap = mark_nav(ctx, broker, asof)

    initial = float(state["initial_cash"] or cfg["initial_cash"])
    total = float(snap["total_value"])
    cash = float(snap["cash"])
    rows = sorted(
        (r for r in db.get_portfolio_snapshots(limit=20000)
         if str(r["snapshot_date"]) <= asof.isoformat()),
        key=lambda r: str(r["snapshot_date"]))
    values = [float(r["total_value"]) for r in rows]
    first_snap = _as_date(rows[0]["snapshot_date"]) if rows else None
    start_d = _as_date(cfg.get("started_at")) or first_snap or asof
    # 净值区间起点:首笔快照日;空仓时快照按墙钟日落库而数据日仍滞后,
    # 故夹到 asof 以内,免得给出一段「未来」的基准区间。
    nav_start = min(first_snap or start_d, asof)
    nav = total / initial if initial else 0.0
    pnl_pct = nav - 1.0
    # 账龄按行情数据日计(与 as_of 同口径):数据断供时不虚增天数
    days_running = max(0, (asof - start_d).days)

    alerts: list[str] = []

    # ---- 基准同期(策略声明的 bench_code,app_config 可覆盖)
    bench: dict = {"code": None, "pnl_pct": None}
    try:
        code = str(cfg.get("bench_code") or "").strip() or bench_code_for(
            ctx, cfg, strat_name)
    except ForwardError:
        code = ""
    if code:
        bench["code"] = code
        series, warn = _bench_frame(ctx, code, args.refresh_bench)
        if warn:
            alerts.append(warn)
        begin = _close_on_or_before(series, nav_start)
        end = _close_on_or_before(series, asof)
        if begin and end:
            bench["start_date"] = begin[0].isoformat()
            bench["end_date"] = end[0].isoformat()
            bench["pnl_pct"] = round(end[1] / begin[1] - 1.0, 6)
            if end[0] == begin[0]:
                bench["pnl_pct"] = 0.0  # 刚建账,同期还没有区间
            if (asof - end[0]).days > STALE_ALERT_DAYS:
                alerts.append(
                    f"基准 {code} 数据截至 {end[0]}(滞后 "
                    f"{(asof - end[0]).days} 自然日),超额含此缺口;"
                    "跑 report --refresh-bench 补齐。")
        else:
            alerts.append(f"基准 {code} 无法对齐起讫日,超额暂缺。")

    excess = (round(pnl_pct - bench["pnl_pct"], 6)
              if bench.get("pnl_pct") is not None else None)

    # ---- 换手(近 30 自然日;单边 = 双边成交额 / 2 / 区间平均净资产)
    cut = (asof - timedelta(days=30)).isoformat()
    buy_amt = sell_amt = 0.0
    n_trades = 0
    for t in db.list_trades(limit=None):
        if str(t["trade_date"]) < cut:
            continue
        amt = float(t["price"]) * int(t["quantity"])
        if t["direction"] == "buy":
            buy_amt += amt
        else:
            sell_amt += amt
        n_trades += 1
    win_vals = [float(r["total_value"]) for r in rows
                if str(r["snapshot_date"]) >= cut] or [total]
    avg_equity = sum(win_vals) / len(win_vals)
    turnover = ((buy_amt + sell_amt) / 2.0 / avg_equity) if avg_equity else 0.0

    # ---- 挂单队列
    pending = db.list_orders(limit=3000, status="pending")
    pending_out = {
        "count": len(pending),
        "buy": sum(1 for o in pending if o["direction"] == "buy"),
        "sell": sum(1 for o in pending if o["direction"] == "sell"),
        "sample": [{"code": o["code"], "direction": o["direction"],
                    "weight": round(float(o["strength"] or 0.0), 6),
                    "queued_at": str(o["created_at"])[:10]}
                   for o in pending[:PENDING_SAMPLE]],
        "truncated": len(pending) > PENDING_SAMPLE,
    }
    last_build = last_build_marker(db)

    # ---- 告警
    lag = (today - asof).days
    if lag > STALE_ALERT_DAYS:
        alerts.append(
            f"行情数据截至 {asof}(滞后 {lag} 自然日):tick 在旧数据上照跑,"
            "但净值/超额不是今天的数。")
    if not values:
        alerts.append("还没有任何净值快照。")
    if last_build and last_build.get("target_n") == 0:
        alerts.append("最近一次建单 tick 没拿到目标权重(上月末参考快照缺失),"
                      "持仓未更新。")
    if last_build and last_build.get("skipped_unaffordable"):
        alerts.append(
            f"{last_build['skipped_unaffordable']} 只成分因整手约束买不起被跳过"
            f"(合计权重 {float(last_build.get('dropped_weight') or 0):.2%});"
            '资金太小复制会退化,可用 init --strategy-config '
            "'{\"min_weight\": 6e-05}' 让策略自己截断重归一。")
    invested = float(snap["market_value"])
    if pending and invested <= 0:
        alerts.append(f"建仓挂单 {len(pending)} 笔待次日开盘成交,当前仍全额现金。")
    elif total and cash / total > 0.05:
        alerts.append(f"滞留现金 {cash / total:.1%}(偏离满仓复制)。")
    mdd = _max_drawdown(values)
    if mdd <= -0.08:
        alerts.append(
            f"组合自起算最大回撤 {mdd:.1%}(前向盘不自动清仓,人工判断)。")
    if days_running >= 60 and excess is not None and excess < 0:
        alerts.append(
            f"运行 {days_running} 天前向超额 {excess:+.2%} 为负 —— 与回测结论"
            "背离,复核复制精度与资金规模(小资金整手退化是已知主因)。")
    if initial and abs(initial - float(cfg["initial_cash"])) > 1.0:
        alerts.append(
            f"账本 initial_cash={initial:,.0f} 与登记值 "
            f"{float(cfg['initial_cash']):,.0f} 不一致(重登未换账户?)。")

    return {
        "account": ctx.account,
        "strategy": strat_name,
        "as_of": asof.isoformat(),
        "data_asof": asof.isoformat(),
        "started_at": start_d.isoformat(),
        "days_running": days_running,
        "nav_days": len(rows),
        "nav": round(nav, 6),
        "total_value": round(total, 2),
        "initial_cash": round(initial, 2),
        "cash": round(cash, 2),
        "market_value": round(invested, 2),
        "pnl_pct": round(pnl_pct, 6),
        "bench": bench,
        "excess": excess,
        "max_drawdown": round(mdd, 6),
        "turnover_last_month": round(turnover, 6),
        "trades_last_month": n_trades,
        "positions": len(snap["positions"]),
        "pending_orders": pending_out,
        "last_build": last_build,
        "alerts": alerts,
    }


# ---------------------------------------------------------------- CLI

def _kv_arg(raw: str | None) -> dict | None:
    """--strategy-config '{"min_weight": 6e-05}' → dict。"""
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise ForwardError(f"--strategy-config 不是合法 JSON:{exc}") from exc
    if not isinstance(obj, dict):
        raise ForwardError("--strategy-config 必须是 JSON 对象")
    return obj


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--account", default="",
                        help="前向账户名(默认取 env QUANTI_ACCOUNT)")
    common.add_argument("--data-dir", default="",
                        help=f"数据目录(默认 env QUANTI_DATA_DIR 或 {ROOT}/data)")
    common.add_argument("--market-db", default="",
                        help="共享行情库路径(默认 <data-dir>/market.db)")
    common.add_argument("--strategies-dir", default=str(ROOT / "strategies"),
                        help="策略目录(默认仓库内 strategies/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", parents=[common],
                       help="建账户 + 登记策略 + 立即建仓")
    p.add_argument("--strategy", required=True, help="strategies/ 里的策略 name")
    p.add_argument("--cash", type=float, required=True, help="初始资金(元)")
    p.add_argument("--bench-code", default="",
                   help="覆盖基准代码(默认用策略声明的 bench_code)")
    p.add_argument("--strategy-config", default="",
                   help='透传给 strategy.init 的 JSON,如 \'{"min_weight": 6e-05}\'')
    p.add_argument("--force", action="store_true",
                   help="在已登记的账户上改登记(保留既有账本,绝不删数据)")

    sub.add_parser("tick", parents=[common], help="推进成交 + 幂等挂单")

    p = sub.add_parser("report", parents=[common], help="输出账户 JSON 报告")
    p.add_argument("--refresh-bench", action="store_true",
                   help="刷新基准指数日线(tushare,打网络;默认不碰外网)")
    p.add_argument("--json-out", default="", help="同时把报告写到该文件")
    return ap


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args(argv)
    if args.cmd == "init" and getattr(args, "strategy_config", ""):
        args.strategy_config_obj = _kv_arg(args.strategy_config)
    else:
        args.strategy_config_obj = None
    try:
        ctx = open_ctx(args)
    except ForwardError as exc:
        print(f"[paper_forward] {exc}", file=sys.stderr)
        return 2
    try:
        try:
            if args.cmd == "init":
                out = cmd_init(ctx, args)
            elif args.cmd == "tick":
                out = cmd_tick(ctx, args)
            else:
                out = cmd_report(ctx, args)
                if args.json_out:
                    Path(args.json_out).write_text(
                        json.dumps(out, ensure_ascii=False, indent=2))
        except ForwardError as exc:
            print(f"[paper_forward] {exc}", file=sys.stderr)
            return 2
    finally:
        ctx.db.close()   # 单次命令一次连接:别把账户库的句柄留在进程里
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
