"""LLM 全权决策模式 (agent_mode="llm_full")。

评估裁定的架构:「LLM 全权决策 + 本地机械执行 + 两根保险丝」。

  * 每日 tick(run_llm_full_decision):选股器 + 策略跑分产出的 ~100 只候选
    连同持仓明细/盈亏/行业/行情摘要/估值/新闻情绪/市场环境一次性交给 LLM;
    LLM 可用 inspect_* 工具对感兴趣的标的按需深挖(60 日行情/估值/财报/新闻),
    最终调用 submit_decision 输出买卖单 + 每个持仓的止损/加仓点位。订单即刻
    执行(盘中实时价成交,见 PaperBroker._buy_now/_sell_now),点位落库。
  * 盘中守护(run_llm_guard_decision):每 llm_guard_interval_sec(默认 300s)
    一轮,持仓明细 + 实时价 + 当前点位交给 LLM,输出卖出/加仓/改点位动作,
    即刻执行。只管持仓,不开新票(开新票是每日 tick 的职权)。
  * 本地机械执行层:LLM 写入 llm_position_plans 的点位由 5 秒机械 guard
    毫秒级比价执行(PaperBroker.check_exits 的 llm 分支 + check_llm_adds)。
    LLM API 失联 = 沿用最后一次成功落库的点位,止损保护永不断档。
  * 保险丝:组合 -30% HWM 熔断(纯机械,不经 LLM)+ 每标的灾难地板
    llm_disaster_floor_pct(仅接点位缺失/被幻觉点位穿透的尾部,UI 可调可关)。

点位本身不受任何 clamp——LLM 定 -5% 还是 -20% 都照执行;买入护栏只剩
RiskManager 的 sanity caps(单票上限/日内开仓数,UI 可调)与市场物理
(涨跌停/整手/参与率/极端高开熔断)。「确保成功」按可实现语义落地:即刻
提交 + 失败原因显式落库上报 + 下一轮 LLM 自见持仓重试;跌停锁板/停牌/T+1
冻结是交易所机制,任何架构无法保证成交。

审计:每轮决策的完整 prompt / 原始输出 / 校验结果 / 执行结果落
agent_decisions(kind="llm_audit" 单独存重型 payload;kind="tick_stage" 记
各阶段轻量事件,UI 时间线用)。LLM 决策不可回放(同输入重跑订单 Jaccard
≈0.4),全量审计是事后分析的唯一原料。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from quanti.agent.goal import Goal
from quanti.agent.llm_runtime import LLMClient, LLMConfig, LLMDecisionLoop
from quanti.agent.signal_pipeline import FusedCandidate
from quanti.data.database import Database
from quanti.execution.base import Broker
from quanti.models import Direction, Signal

logger = logging.getLogger(__name__)

FULL_SYSTEM = """你是一名 A 股全权交易员(quanti 系统 LLM 全权模式)。系统已把\
候选池(选股器+经典策略跑分的 top 名单)、当前持仓、账户与市场环境全部给你,\
最终买卖决策与每个持仓的止损/加仓点位完全由你决定——没有别的算法替你把关,\
你的输出会被直接执行。

决策框架(每个标的综合考虑):
1. 技术面:趋势(均线排列)、动量(5/20 日涨幅)、量能(量比)、支撑/压力;
2. 基本面:估值(PE/PB)、盈利能力与增速(ROE、净利/营收同比);
3. 消息面:近期新闻情绪(候选行内附分数;可用 inspect_candidate 看标题);
4. 组合层面:行业集中度、既有持仓盈亏与仓位、现金水位、市场环境(regime)。

工具用法:候选行是紧凑摘要;对进入你候选短名单的标的,先用 inspect_candidate \
拉 60 日行情摘要/估值/财报要点/新闻标题再定夺,不要只凭一行摘要重仓。

输出要求(调用 submit_decision 恰好一次):
* orders:买入(必须来自候选列表或对现有持仓加仓)与卖出(必须是现有持仓)。\
size_pct 是目标仓位占总资产比例,须在系统给出的单票上限内;买入金额至少要买\
得起 1 手(100 股),现价×100 就是一手的钱。没有值得做的就给空列表。
* plans:必须覆盖【每一个】现有持仓(含本轮新买入的),给出止损价 stop_price\
(现价跌破即由本地机械守护在 5 秒内卖出,这是唯一的日常止损——你不设、设错,\
就只剩灾难地板兜底)与可选的加仓价 add_price/加仓幅度 add_size_pct(现价回落\
至该价即机械加仓;不想加仓就给 0)。点位用当前价格轴(与输入的现价同轴)。
* 每个 order/plan 附一句简短中文理由;reasoning 给本轮整体思路(≤300 字)。

约束:A 股 T+1(今日买入明日才能卖)、100 股整手、涨跌停不可成交。\
卖出优先于买入执行(先释放现金)。你是唯一决策者,理由要经得起复盘。"""

GUARD_SYSTEM = """你是 A 股盘中风控守护(quanti LLM 全权模式)。每隔几分钟你会\
看到最新持仓明细(实时价/盈亏/今日涨幅/当前止损与加仓点位)。你的职权:
* 卖出(sells):形态破位、放量跳水、消息恶化等,立即市价卖出;
* 加仓(adds):回调到位且逻辑未变,立即市价加仓(受单票上限约束);
* 改点位(plans):上移止损锁盈、调整加仓价——本地机械守护每 5 秒按你落库的\
点位比价执行,这是两轮之间唯一的保护,点位必须始终合理。

不开新票(开新票是每日决策的职权)。无需动作时给空列表——盘中大多数时刻\
本就该按兵不动,频繁折腾只会磨损成本。调用 submit_guard_actions 恰好一次,\
动作附一句中文理由。约束:T+1、整手、涨跌停不可成交。"""

CLOSE_REPLAN_SYSTEM = """你是 A 股收盘后风控点位复核员(quanti LLM 全权模式)。\
当日已收盘,你看到每个持仓的最新明细(收盘价/盈亏/当前止损与加仓点位)。你的\
唯一职权是基于当日收盘为每个持仓重算明日点位:
* stop_price(每个持仓必给,一个都不许漏):趋势完好可上移锁盈,破位风险高\
可收紧;必须低于收盘价——高于现价的"止损"等于卖出指令,不归你管;
* add_price / add_size_pct(可选):回调加仓点位,加仓逻辑不再成立就别给。

不买不卖——买卖是每日决策的职权,你只管点位。明日盘中本地机械守护每 5 秒\
按你落库的点位比价执行,这是明日全天唯一的保护。调用 submit_close_plans \
恰好一次,每条附一句中文理由。"""


# ------------------------------------------------------------- tools

def _tools_full(max_size_pct: float, max_orders: int) -> list[dict]:
    plan_item = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "stop_price": {"type": "number", "minimum": 0,
                           "description": "止损价,现价跌破即机械卖出;必填>0"},
            "add_price": {"type": "number", "minimum": 0,
                          "description": "加仓触发价,0=不加仓"},
            "add_size_pct": {"type": "number", "minimum": 0,
                             "maximum": max(0.01, max_size_pct),
                             "description": "加仓目标占总资产比例"},
            "reason": {"type": "string", "maxLength": 80},
        },
        "required": ["code", "stop_price"],
    }
    return [
        {
            "name": "inspect_candidate",
            "description": "深挖一只候选/持仓:近60日行情摘要(均线/涨幅/量比/"
                           "高低点)、估值(PE/PB/市值)、最新财报要点(ROE/净利"
                           "与营收同比)、近7日新闻标题。",
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
        {
            "name": "inspect_position",
            "description": "查看一只当前持仓的明细(数量/成本/盈亏/冻结)。",
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
        {
            "name": "inspect_decision_history",
            "description": "最近 N 条系统决策记录(为何买入/卖出某票)。",
            "input_schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1,
                                         "maximum": 20}},
                "required": ["limit"],
            },
        },
        {
            "name": "submit_decision",
            "description": f"提交本轮最终决策,调用恰好一次。orders ≤ {max_orders} "
                           "笔;plans 必须覆盖每一个现有持仓(含本轮新买入)。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "orders": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "direction": {"type": "string",
                                              "enum": ["buy", "sell"]},
                                "size_pct": {
                                    "type": "number", "minimum": 0.01,
                                    "maximum": max(0.01, max_size_pct),
                                    "description": "buy 必填;sell 忽略(全平)"},
                                "reason": {"type": "string", "maxLength": 80},
                            },
                            "required": ["code", "direction", "reason"],
                        },
                    },
                    "plans": {"type": "array", "items": plan_item},
                    "reasoning": {"type": "string",
                                  "description": "本轮整体思路,≤300字,UI 展示"},
                },
                "required": ["orders", "plans"],
            },
        },
    ]


def _tools_guard(max_size_pct: float) -> list[dict]:
    plan_item = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "stop_price": {"type": "number", "minimum": 0},
            "add_price": {"type": "number", "minimum": 0},
            "add_size_pct": {"type": "number", "minimum": 0,
                             "maximum": max(0.01, max_size_pct)},
            "reason": {"type": "string", "maxLength": 80},
        },
        "required": ["code", "stop_price"],
    }
    # inspect_position 一并挂上:≥2 个 tool 时 openai_compat 用 tool_choice=
    # "auto",DeepSeek v4 的 thinking 得以保留(单 tool 会被强制 tool_choice
    # 进而静默关 thinking——盘中风控恰是最需要推理的场景)。
    return [
        {
            "name": "inspect_position",
            "description": "查看一只当前持仓的明细(数量/成本/盈亏/冻结)。",
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
        {
            "name": "submit_guard_actions",
            "description": "提交本轮盘中动作,调用恰好一次。无需动作时三个列表"
                           "均给空。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "sells": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "reason": {"type": "string", "maxLength": 80},
                            },
                            "required": ["code", "reason"],
                        },
                    },
                    "adds": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "size_pct": {"type": "number", "minimum": 0.01,
                                             "maximum": max(0.01, max_size_pct)},
                                "reason": {"type": "string", "maxLength": 80},
                            },
                            "required": ["code", "size_pct", "reason"],
                        },
                    },
                    "plans": {"type": "array", "items": plan_item,
                              "description": "要改写点位的持仓;不改的不用给"},
                    "reasoning": {"type": "string", "maxLength": 300},
                },
                "required": ["sells", "adds", "plans"],
            },
        },
    ]


def _tools_close_replan(max_size_pct: float) -> list[dict]:
    plan_item = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "stop_price": {"type": "number", "minimum": 0},
            "add_price": {"type": "number", "minimum": 0},
            "add_size_pct": {"type": "number", "minimum": 0,
                             "maximum": max(0.01, max_size_pct)},
            "reason": {"type": "string", "maxLength": 80},
        },
        "required": ["code", "stop_price"],
    }
    # inspect_position 一并挂上,理由同 _tools_guard(≥2 tool 保 thinking)。
    return [
        {
            "name": "inspect_position",
            "description": "查看一只当前持仓的明细(数量/成本/盈亏/冻结)。",
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
        {
            "name": "submit_close_plans",
            "description": "提交收盘后重算的持仓点位,调用恰好一次;"
                           "每个持仓都必须给出 stop_price。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "plans": {"type": "array", "items": plan_item},
                    "reasoning": {"type": "string", "maxLength": 300},
                },
                "required": ["plans"],
            },
        },
    ]


# ------------------------------------------------------- context builders

def _bars_summary(provider, code: str, days: int = 130) -> dict:
    """近 60 交易日行情摘要(inspect_candidate 与候选行共用的底层数据)。"""
    end = date.today()
    bars = provider.get_daily_bars(code, end - timedelta(days=days), end)
    if not bars:
        return {}
    closes = [float(b.close) for b in bars]
    vols = [float(b.volume or 0) for b in bars]

    def ma(n: int) -> float | None:
        return round(sum(closes[-n:]) / n, 3) if len(closes) >= n else None

    def chg(n: int) -> float | None:
        return (round(closes[-1] / closes[-n - 1] - 1, 4)
                if len(closes) > n and closes[-n - 1] else None)

    vol_ratio = None
    if len(vols) >= 20 and sum(vols[-20:]) > 0:
        vol_ratio = round((sum(vols[-5:]) / 5) / (sum(vols[-20:]) / 20), 2)
    recent = bars[-60:]
    return {
        "close": round(closes[-1], 3), "date": bars[-1].date.isoformat(),
        "chg_5d": chg(5), "chg_20d": chg(20), "chg_60d": chg(60),
        "ma5": ma(5), "ma10": ma(10), "ma20": ma(20), "ma60": ma(60),
        "high_60d": round(max(float(b.high) for b in recent), 3),
        "low_60d": round(min(float(b.low) for b in recent), 3),
        "vol_ratio_5v20": vol_ratio,
    }


def _valuation(db: Database, code: str) -> dict:
    """最新估值行(PE/PB/市值)。daily_basic 的市值单位是万元 → 换算亿元。"""
    end = date.today()
    try:
        df = db.get_daily_basic(code, end - timedelta(days=14), end)
    except Exception:  # noqa: BLE001 - 估值缺失不阻塞决策
        return {}
    if df is None or df.empty:
        return {}
    r = df.iloc[-1]

    def f(k):
        v = r.get(k)
        return None if v is None or v != v else round(float(v), 2)

    mv = f("total_mv")
    return {"pe_ttm": f("pe_ttm") or f("pe"), "pb": f("pb"),
            "total_mv_yi": round(mv / 1e4, 1) if mv else None}


def _fundamentals(db: Database, code: str) -> dict:
    """PIT 视角下最新一期财报要点。"""
    try:
        df = db.get_financials_asof(code, date.today())
    except Exception:  # noqa: BLE001
        return {}
    if df is None or df.empty:
        return {}
    r = df.iloc[-1]

    def f(k):
        v = r.get(k)
        return None if v is None or v != v else round(float(v), 2)

    return {"end_date": str(r.get("end_date", "")), "roe": f("roe"),
            "netprofit_yoy": f("netprofit_yoy"),
            "revenue_yoy": f("revenue_yoy")}


def candidate_detail(db: Database, provider, code: str) -> str:
    """inspect_candidate 的实现:行情摘要 + 估值 + 财报 + 新闻标题(JSON)。"""
    stock = db.get_stock(code)
    out: dict = {
        "code": code,
        "name": stock.name if stock else "",
        "industry": stock.industry if stock else "",
        "bars": _bars_summary(provider, code),
        "valuation": _valuation(db, code),
        "fundamentals": _fundamentals(db, code),
    }
    try:
        from quanti.agent.sentiment import fetch_recent_news
        out["news"] = [n["title"] for n in fetch_recent_news(code, limit=5)]
    except Exception as e:  # noqa: BLE001 - 新闻源挂了不阻塞决策
        out["news_error"] = str(e)
    return json.dumps(out, ensure_ascii=False)


def _positions_block(db: Database, portfolio: dict,
                     realtime: dict[str, float] | None = None) -> list[str]:
    """持仓明细行(含当前 LLM 点位),tick 与 guard 共用。"""
    plans = {p["code"]: p for p in db.list_llm_plans()}
    total_v = float(portfolio.get("total_value", 0) or 0)
    lines: list[str] = []
    positions = portfolio.get("positions", []) or []
    for p in sorted(positions, key=lambda x: x.get("code", "")):
        code = p.get("code", "")
        price = float((realtime or {}).get(code)
                      or p.get("current_price", 0) or 0)
        w = (float(p.get("market_value", 0) or 0) / total_v) if total_v else 0.0
        plan = plans.get(code) or {}
        stop = float(plan.get("stop_price") or 0)
        addp = float(plan.get("add_price") or 0)
        lines.append(
            f"  - {code} {p.get('name', '')} 行业={p.get('industry') or '未知'}"
            f" x{p.get('quantity', 0)} 均价{p.get('avg_cost', 0):.2f}"
            f" 现价{price:.2f} 盈亏{p.get('pnl_pct', 0):+.2%} 占比{w:.1%}"
            f" 买入日{p.get('buy_date') or '?'}"
            f" | 止损点位{'%.2f' % stop if stop > 0 else '未设'}"
            f" 加仓点位{'%.2f' % addp if addp > 0 else '无'}")
    if not lines:
        lines.append("  - (空仓)")
    return lines


def _account_block(portfolio: dict, risk_limits: dict) -> list[str]:
    total_v = float(portfolio.get("total_value", 0) or 0)
    cash_v = float(portfolio.get("cash", 0) or 0)
    lines = [
        "# 账户",
        f"- 总资产: ¥{total_v:,.0f}  现金: ¥{cash_v:,.0f}"
        + (f" ({cash_v / total_v:.0%})" if total_v else ""),
        f"- 累计盈亏: {portfolio.get('pnl_pct', 0):+.2%}",
        f"- 单票上限 {risk_limits.get('max_position_pct', 0.2):.0%}"
        f" · 日内开仓上限 {risk_limits.get('max_daily_trades', 20)} 笔"
        f" · 组合熔断 {risk_limits.get('portfolio_stop_loss_pct', -0.3):.0%}"
        f" · 灾难地板 {risk_limits.get('llm_disaster_floor_pct', -0.25):.0%}"
        f"(仅兜底,日常止损=你的点位)",
        f"- 1 手 = 100 股;时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ]
    return lines


def build_full_context(db: Database, goal: Goal, portfolio: dict,
                       candidates: list[FusedCandidate], provider,
                       risk_limits: dict, max_candidates: int = 100,
                       realtime: dict[str, float] | None = None) -> str:
    """realtime:今日实时价 overlay(hfq 轴,broker.context_marks)。此前
    tick 的候选/持仓「现价」全是上一交易日收盘——决策按昨收框架做、成交
    却按今日实时价,当日大涨时 LLM 以为的回调买入实际买在高位(2026-08-20
    000703 实发,用户拍板选 B 修)。有实时价的标的标「今」并附较昨收涨跌,
    没有的仍标「昨收」;日线摘要(均线/N日涨幅)保持昨收口径不混轴。"""
    realtime = realtime or {}
    lines: list[str] = []
    lines += _account_block(portfolio, risk_limits)
    lines.append("")
    lines.append("# 当前持仓 (含现行 LLM 点位)")
    lines += _positions_block(db, portfolio, realtime)
    lines.append("")

    lines.append(f"# 候选股 (top {min(len(candidates), max_candidates)}, "
                 "按综合分降序;PE/PB 空=缺数据)")
    if realtime:
        lines.append("(现价标「今」= 今日实时价,括号内为较昨收涨跌;"
                     "5日/20日涨幅与均线仍按昨收口径)")
    for c in candidates[:max_candidates]:
        code = c.code
        stock = db.get_stock(code)
        name = stock.name if stock else ""
        bs = _bars_summary(provider, code)
        va = _valuation(db, code)
        sent = (f" 情绪{c.sentiment_score:+.2f}"
                if getattr(c, "sentiment_score", 0.0) else "")
        close = bs.get("close") or float(getattr(c, "current_price", 0) or 0)
        rt = float(realtime.get(code) or 0)
        if rt > 0 and close:
            price_part = f"今¥{rt:.2f}({rt / close - 1:+.1%})"
        elif rt > 0:
            price_part = f"今¥{rt:.2f}"
        else:
            price_part = f"昨收¥{close:.2f}"
        chg5 = bs.get("chg_5d")
        chg20 = bs.get("chg_20d")
        parts = [f"- {code} {name}", f"分{c.final_score:.2f}", price_part]
        if chg5 is not None:
            parts.append(f"5日{chg5:+.1%}")
        if chg20 is not None:
            parts.append(f"20日{chg20:+.1%}")
        if bs.get("vol_ratio_5v20") is not None:
            parts.append(f"量比{bs['vol_ratio_5v20']}")
        if va.get("pe_ttm") is not None:
            parts.append(f"PE{va['pe_ttm']}")
        if va.get("pb") is not None:
            parts.append(f"PB{va['pb']}")
        parts.append(f"行业={c.industry or (stock.industry if stock else '') or '未知'}")
        strats = "+".join(c.contributing_strategies) or "因子"
        parts.append(f"策略={strats}{sent}")
        lines.append(" | ".join(parts))
    if not candidates:
        lines.append("- (本轮无候选)")
    lines.append("")

    lines.append("# 最近决策")
    for d in db.list_decisions(limit=5):
        lines.append(f"- [{d.get('ts', '')[:16]}] {d.get('kind', '')}: "
                     f"{d.get('summary', '')[:100]}")
    lines.append("")
    lines.append("请按系统提示深挖后调用 submit_decision(plans 必须覆盖全部持仓)。")
    return "\n".join(lines)


def build_guard_context(db: Database, portfolio: dict,
                        realtime: dict[str, float],
                        risk_limits: dict, provider) -> str:
    lines: list[str] = []
    lines += _account_block(portfolio, risk_limits)
    lines.append("")
    lines.append("# 持仓明细 (实时价; 含现行点位)")
    lines += _positions_block(db, portfolio, realtime)
    # 今日涨幅:实时价 vs 昨收。
    lines.append("")
    lines.append("# 今日盘中变动")
    for p in portfolio.get("positions", []) or []:
        code = p.get("code", "")
        rt = realtime.get(code)
        bs = _bars_summary(provider, code, days=40)
        prev = bs.get("close")  # 盘中最新 bar 即昨日收盘
        if rt and prev:
            lines.append(f"  - {code} 今日 {rt / prev - 1:+.2%}"
                         f" (昨收{prev:.2f} → 现价{rt:.2f})")
    lines.append("")
    lines.append("请评估是否需要动作,调用 submit_guard_actions"
                 "(多数时刻应按兵不动,空列表即可)。")
    return "\n".join(lines)


# ------------------------------------------------------- validation

def validate_decision(terminal: dict, held: set[str],
                      candidate_codes: set[str], max_size_pct: float,
                      max_orders: int) -> tuple[list[dict], list[dict], list[str]]:
    """sanity 闸(保骨架不设 alpha 过滤):成员校验 + size 上限 + 单数 cap。

    Returns (valid_orders, valid_plans, rejects)。plans 的 code 须属于
    持仓∪本轮买入;点位数值不做区间 clamp(点位完全归 LLM),但必须可解析。
    """
    rejects: list[str] = []
    orders_in = list(terminal.get("orders") or [])
    plans_in = list(terminal.get("plans") or [])

    valid_orders: list[dict] = []
    for o in orders_in:
        code = str(o.get("code", "")).strip()
        direction = str(o.get("direction", "")).lower()
        if direction == "sell":
            if code not in held:
                rejects.append(f"sell {code}: 非持仓")
                continue
            valid_orders.append({"code": code, "direction": "sell",
                                 "reason": str(o.get("reason", ""))[:80]})
            continue
        if direction != "buy":
            rejects.append(f"{code}: 未知方向 {direction}")
            continue
        if code not in candidate_codes and code not in held:
            rejects.append(f"buy {code}: 不在候选与持仓中(幻觉代码?)")
            continue
        try:
            size_pct = float(o.get("size_pct", 0))
        except (TypeError, ValueError):
            rejects.append(f"buy {code}: size_pct 不可解析")
            continue
        if not (0 < size_pct <= max_size_pct):
            rejects.append(
                f"buy {code}: size_pct={size_pct} 超出 (0, {max_size_pct:.2f}]")
            continue
        valid_orders.append({"code": code, "direction": "buy",
                             "size_pct": size_pct,
                             "reason": str(o.get("reason", ""))[:80]})
    if len(valid_orders) > max_orders:
        rejects.append(f"订单数 {len(valid_orders)} 超上限 {max_orders},截断")
        valid_orders = valid_orders[:max_orders]

    buy_codes = {o["code"] for o in valid_orders if o["direction"] == "buy"}
    plan_scope = held | buy_codes
    valid_plans: list[dict] = []
    for p in plans_in:
        code = str(p.get("code", "")).strip()
        if code not in plan_scope:
            rejects.append(f"plan {code}: 非持仓/非本轮买入")
            continue
        try:
            stop = float(p.get("stop_price", 0) or 0)
            addp = float(p.get("add_price", 0) or 0)
            adds = float(p.get("add_size_pct", 0) or 0)
        except (TypeError, ValueError):
            rejects.append(f"plan {code}: 点位不可解析")
            continue
        valid_plans.append({"code": code, "stop_price": stop,
                            "add_price": addp,
                            "add_size_pct": min(adds, max_size_pct),
                            "reason": str(p.get("reason", ""))[:80]})
    return valid_orders, valid_plans, rejects


def _persist_plans(db: Database, plans: list[dict],
                   held_after: set[str]) -> tuple[int, list[str]]:
    """点位落库;返回 (落库数, 未被覆盖的持仓 code 列表 = LLM 输出缺漏)。"""
    covered: set[str] = set()
    for p in plans:
        db.upsert_llm_plan(p["code"], p["stop_price"], p["add_price"],
                           p["add_size_pct"], p.get("reason", ""))
        covered.add(p["code"])
    missing = sorted(held_after - covered)
    return len(covered), missing


# ------------------------------------------------------- orchestrators

def _stage(db: Database, tick_ts: str, phase: str, stage: str, summary: str,
           **details) -> None:
    """tick 流程时间线事件(UI 按 tick_ts 聚组渲染)。轻量,永不 raise。"""
    try:
        db.log_decision("tick_stage", summary,
                        details={"tick_ts": tick_ts, "phase": phase,
                                 "stage": stage, **details})
    except Exception as e:  # noqa: BLE001 - 观测不许打断决策链
        logger.warning("tick_stage log failed: %s", e)


def _risk_limits(db: Database) -> dict:
    from quanti.risk.manager import RiskConfig, risk_config_from_dict
    try:
        rc = risk_config_from_dict(db.get_risk_config())
    except Exception:  # noqa: BLE001
        rc = RiskConfig()
    return {
        "max_position_pct": rc.max_position_pct,
        "max_daily_trades": rc.max_daily_trades,
        "portfolio_stop_loss_pct": rc.portfolio_stop_loss_pct,
        "llm_disaster_floor_pct": rc.llm_disaster_floor_pct,
    }


def _orders_to_signals(orders: list[dict], max_size_pct: float,
                       dominant_by_code: dict[str, str],
                       tag: str) -> tuple[list[Signal], list[Signal]]:
    """(sells, buys)。sell 全平;buy 的 strength = size_pct/单票上限(broker
    的 sizing 语义,与经典 LLM 路径一致)。"""
    sells: list[Signal] = []
    buys: list[Signal] = []
    for o in orders:
        if o["direction"] == "sell":
            sells.append(Signal(
                stock_code=o["code"], direction=Direction.SELL, strength=1.0,
                reason=f"{tag}: {o.get('reason', '')}"))
        else:
            buys.append(Signal(
                stock_code=o["code"], direction=Direction.BUY,
                strength=min(1.0, float(o["size_pct"]) / max(0.01, max_size_pct)),
                reason=f"{tag}: {o.get('reason', '')}",
                entry_strategy=dominant_by_code.get(o["code"], "")))
    return sells, buys


def run_llm_full_decision(
    *,
    db: Database,
    broker: Broker,
    provider,
    goal: Goal,
    candidates: list[FusedCandidate],
    llm_client: LLMClient,
    cfg: LLMConfig | None = None,
    broker_gate=None,
) -> dict:
    """每日 tick 的 LLM 全权决策。LLM I/O 全程锁外;执行尾巴经 broker_gate。"""
    from contextlib import nullcontext

    cfg = cfg or LLMConfig()
    tick_ts = datetime.now().isoformat()
    risk_limits = _risk_limits(db)
    cfg.max_size_pct = float(risk_limits["max_position_pct"])

    portfolio = broker.snapshot_portfolio()
    held = {p["code"] for p in portfolio.get("positions", [])}
    candidate_codes = {c.code for c in candidates}
    _stage(db, tick_ts, "tick", "candidates",
           f"候选 {len(candidates)} 只、持仓 {len(held)} 只,进入 LLM 决策",
           n_candidates=len(candidates), n_positions=len(held))

    # 今日实时价 overlay(hfq 轴):候选∪持仓一次批量拉(候选 100 只 ≈ 2
    # 次请求,每日一次)。拉不到(开盘前/源全挂)= 空 dict,上下文自动
    # 落回昨收——行为等同修复前,永不阻塞 tick。
    realtime: dict[str, float] = {}
    try:
        marks_fn = getattr(broker, "context_marks", None)
        if callable(marks_fn):
            realtime = marks_fn(
                sorted(candidate_codes | held)) or {}
    except Exception as e:  # noqa: BLE001 - 上下文增强,失败不许打断决策
        logger.warning("context realtime marks skipped: %s", e)

    ctx = build_full_context(db, goal, portfolio, candidates, provider,
                             risk_limits,
                             max_candidates=cfg.max_candidates_in_context,
                             realtime=realtime)
    # 市场环境(regime)块:复用既有注入通道(客观读数 + 陈旧闸)。
    try:
        from quanti.regime.prompt import regime_block
        regime_ctx, _meta = regime_block(
            db, goal, broker, provider=provider)
        if regime_ctx:
            ctx += regime_ctx + "\n\n请基于以上信息调用 submit_decision。"
    except Exception as e:  # noqa: BLE001
        logger.warning("regime block skipped: %s", e)

    def dispatcher(name: str, inp: dict) -> str:
        if name == "inspect_candidate":
            return candidate_detail(db, provider, str(inp.get("code", "")))
        if name == "inspect_position":
            code = str(inp.get("code", ""))
            pos = next((p for p in portfolio.get("positions", [])
                        if p.get("code") == code), None)
            if pos is None:
                return json.dumps({"held": False, "code": code})
            return json.dumps({"held": True,
                               **{k: v for k, v in pos.items()
                                  if isinstance(v, (int, float, str))}},
                              ensure_ascii=False)
        if name == "inspect_decision_history":
            limit = max(1, min(int(inp.get("limit", 5)), 20))
            return json.dumps([{"ts": d.get("ts"), "kind": d.get("kind"),
                                "summary": d.get("summary")}
                               for d in db.list_decisions(limit=limit)],
                              ensure_ascii=False)
        return json.dumps({"error": f"unknown tool {name}"})

    tools = _tools_full(cfg.max_size_pct, cfg.max_orders)
    loop = LLMDecisionLoop(llm_client, cfg, terminal_tool="submit_decision",
                           system_prompt=FULL_SYSTEM, tools=tools)
    terminal, reasoning, debug = loop.run(ctx, dispatcher)
    # 截断硬拒(PR#149 教训):max_tokens 截断的输出不可信,整轮弃 + 重试一次。
    if debug.get("stop_reason") == "max_tokens" and not terminal:
        _stage(db, tick_ts, "tick", "llm_retry",
               "LLM 输出被 max_tokens 截断,重试一次")
        terminal, reasoning, debug = loop.run(ctx, dispatcher)

    _stage(db, tick_ts, "tick", "llm_done",
           f"LLM 决策完成: {len(terminal.get('orders') or [])} 单提议, "
           f"{len(terminal.get('plans') or [])} 个点位",
           usage=debug.get("usage", {}), iterations=debug.get("iterations", 0),
           error=debug.get("error", ""))

    orders, plans, rejects = validate_decision(
        terminal, held, candidate_codes, cfg.max_size_pct, cfg.max_orders)
    if rejects:
        _stage(db, tick_ts, "tick", "validate",
               f"sanity 闸拦下 {len(rejects)} 项", rejects=rejects)

    dominant_by_code = {c.code: c.dominant_strategy for c in candidates}
    sells, buys = _orders_to_signals(orders, cfg.max_size_pct,
                                     dominant_by_code, "LLM")

    with (broker_gate or nullcontext)():
        sell_result = broker.execute_signals(sells, strategy_name="llm_full") \
            if sells else None
        buy_result = broker.execute_signals(buys, strategy_name="llm_full") \
            if buys else None
        snapshot = broker.snapshot_portfolio()
        held_after = {p["code"] for p in snapshot.get("positions", [])}
        n_plans, missing = _persist_plans(db, plans, held_after)

    filled = ((sell_result.filled if sell_result else 0)
              + (buy_result.filled if buy_result else 0))
    pending = ((sell_result.pending if sell_result else 0)
               + (buy_result.pending if buy_result else 0))
    rejected = ((sell_result.rejected if sell_result else 0)
                + (buy_result.rejected if buy_result else 0))
    reject_reasons = list(dict.fromkeys(
        (sell_result.reasons if sell_result else [])
        + (buy_result.reasons if buy_result else [])))
    _stage(db, tick_ts, "tick", "execute",
           f"执行: {filled} 成交, {pending} 挂单, {rejected} 拒单",
           filled=filled, pending=pending, rejected=rejected,
           reject_reasons=reject_reasons)
    if missing:
        _stage(db, tick_ts, "tick", "plans_missing",
               f"LLM 未给 {len(missing)} 个持仓设点位(灾难地板兜底): "
               + ", ".join(missing), missing=missing)
    _stage(db, tick_ts, "tick", "plans",
           f"落库 {n_plans} 个点位计划", n_plans=n_plans)

    # 重型审计行:完整 prompt + 原始终结输出。单独 kind,列表接口默认过滤。
    db.log_decision(
        "llm_audit", f"LLM 全权决策审计 {tick_ts}",
        details={
            "tick_ts": tick_ts, "phase": "tick",
            "model": getattr(llm_client, "resolved_model",
                             lambda m: m)(cfg.model),
            "prompt": ctx, "terminal_input": terminal,
            "reasoning": reasoning, "rejects": rejects,
            "usage": debug.get("usage", {}),
            "stop_reason": debug.get("stop_reason", ""),
            "tool_calls": [t.get("name") for t in debug.get("tool_calls", [])],
        })

    summary_bits = [f"{len(orders)} 单", f"{filled} 成交"]
    if pending:
        summary_bits.append(f"{pending} 挂单")
    if rejected:
        summary_bits.append(f"{rejected} 拒单")
    summary_bits.append(f"{n_plans} 点位")
    summary = "LLM 全权决策: " + ", ".join(summary_bits)
    if reasoning:
        summary += f" — {reasoning[:120]}"
    db.log_decision("llm_cycle", summary, details={
        "tick_ts": tick_ts, "mode": "llm_full",
        "n_candidates": len(candidates),
        "n_proposed": len(terminal.get("orders") or []),
        "n_valid": len(orders), "rejections": rejects,
        "filled": filled, "n_pending": pending, "n_rejected": rejected,
        "reject_reasons": reject_reasons,
        "plans": plans, "plans_missing": missing,
        "reasoning": reasoning,
        "usage": debug.get("usage", {}),
        "order_codes": [o["code"] for o in orders],
    })
    return {"ok": True, "signals": len(orders), "filled": filled,
            "pending": pending, "rejected": rejected,
            "stop_loss_filled": 0, "reasoning": reasoning,
            "llm_orders": orders, "plans": plans, "snapshot": snapshot,
            "debug": debug}


def run_llm_guard_decision(
    *,
    db: Database,
    broker: Broker,
    provider,
    goal: Goal,
    llm_client: LLMClient,
    cfg: LLMConfig | None = None,
    broker_gate=None,
) -> dict:
    """盘中 LLM 守护一轮:持仓管理(卖出/加仓/改点位),不开新票。

    LLM I/O 全程在 broker 锁外;执行尾巴经 broker_gate 进临界区,且入区后
    重新对账持仓(快照可能已被 5 秒机械 guard 改写——PR#148 锁纪律)。
    """
    from contextlib import nullcontext

    cfg = cfg or LLMConfig()
    tick_ts = datetime.now().isoformat()
    risk_limits = _risk_limits(db)
    cfg.max_size_pct = float(risk_limits["max_position_pct"])

    portfolio = broker.snapshot_portfolio()
    positions = portfolio.get("positions", []) or []
    if not positions:
        return {"ok": True, "skipped": "空仓,无需守护"}
    held = {p["code"] for p in positions}
    realtime = {p["code"]: float(p.get("current_price") or 0)
                for p in positions}

    ctx = build_guard_context(db, portfolio, realtime, risk_limits, provider)

    def dispatcher(name: str, inp: dict) -> str:
        if name == "inspect_position":
            code = str(inp.get("code", ""))
            pos = next((p for p in positions if p.get("code") == code), None)
            if pos is None:
                return json.dumps({"held": False, "code": code})
            return json.dumps({"held": True,
                               **{k: v for k, v in pos.items()
                                  if isinstance(v, (int, float, str))}},
                              ensure_ascii=False)
        return json.dumps({"error": f"unknown tool {name}"})

    loop = LLMDecisionLoop(llm_client, cfg,
                           terminal_tool="submit_guard_actions",
                           system_prompt=GUARD_SYSTEM,
                           tools=_tools_guard(cfg.max_size_pct))
    terminal, reasoning, debug = loop.run(ctx, dispatcher)
    if debug.get("error"):
        db.log_decision("llm_guard_skip",
                        f"盘中 LLM 守护本轮跳过(调用失败): {debug['error'][:120]}",
                        details={"error": debug.get("error", "")})
        return {"ok": False, "error": debug.get("error", "")}
    if debug.get("stop_reason") == "max_tokens" and not terminal:
        db.log_decision("llm_guard_skip",
                        "盘中 LLM 守护本轮跳过(输出截断)",
                        details={"stop_reason": "max_tokens"})
        return {"ok": False, "error": "truncated"}

    # 归一到 validate_decision 的 orders 形状。
    orders_in = ([{"code": s.get("code"), "direction": "sell",
                   "reason": s.get("reason", "")}
                  for s in (terminal.get("sells") or [])]
                 + [{"code": a.get("code"), "direction": "buy",
                     "size_pct": a.get("size_pct"),
                     "reason": a.get("reason", "")}
                    for a in (terminal.get("adds") or [])])
    orders, plans, rejects = validate_decision(
        {"orders": orders_in, "plans": terminal.get("plans") or []},
        held, held, cfg.max_size_pct, cfg.max_orders)

    sells, buys = _orders_to_signals(orders, cfg.max_size_pct, {}, "LLM守护")

    with (broker_gate or nullcontext)():
        # 临界区内重新对账:一只票可能已被机械止损卖掉。
        now_held = {p["code"] for p in db.list_positions()}
        sells = [s for s in sells if s.stock_code in now_held]
        buys = [b for b in buys if b.stock_code in now_held]
        sell_result = broker.execute_signals(sells, strategy_name="llm_guard") \
            if sells else None
        buy_result = broker.execute_signals(buys, strategy_name="llm_guard") \
            if buys else None
        n_plans, _missing = _persist_plans(
            db, [p for p in plans if p["code"] in now_held], set())

    filled = ((sell_result.filled if sell_result else 0)
              + (buy_result.filled if buy_result else 0))
    rejected = ((sell_result.rejected if sell_result else 0)
                + (buy_result.rejected if buy_result else 0))
    reject_reasons = list(dict.fromkeys(
        (sell_result.reasons if sell_result else [])
        + (buy_result.reasons if buy_result else [])))

    acted = bool(sells or buys or n_plans)
    if acted or rejects:
        summary = (f"盘中 LLM 守护: {len(sells)} 卖, {len(buys)} 加, "
                   f"{n_plans} 改点位, {filled} 成交")
        if rejected:
            summary += f", {rejected} 拒单"
            if reject_reasons:
                summary += f"({'; '.join(reject_reasons)[:80]})"
        if reasoning:
            summary += f" — {reasoning[:100]}"
        db.log_decision("llm_guard", summary, details={
            "tick_ts": tick_ts, "sells": [s.stock_code for s in sells],
            "adds": [b.stock_code for b in buys], "plans": plans,
            "rejects": rejects, "filled": filled, "rejected": rejected,
            "reject_reasons": reject_reasons, "reasoning": reasoning,
            "usage": debug.get("usage", {}),
        })
    return {"ok": True, "sells": len(sells), "adds": len(buys),
            "plans": n_plans, "filled": filled, "rejected": rejected}


def run_llm_close_replan(
    *,
    db: Database,
    broker: Broker,
    provider,
    goal: Goal,
    llm_client: LLMClient,
    cfg: LLMConfig | None = None,
) -> dict:
    """收盘后点位重算:LLM 基于当日收盘为每个持仓重算止损/加仓点位,只落
    llm_position_plans,不下任何单——明日盘中由 5 秒机械守护按价执行。

    返回 {"ok": bool, ...}。ok=False = 本轮没算成(LLM 调用失败/截断/有持仓
    没拿到点位),调用方(background_sync)负责打日志、告警与退避重试直到
    当天成功——「确保成功」的重试语义在调度层,本函数保持单轮纯粹。
    """
    cfg = cfg or LLMConfig()
    risk_limits = _risk_limits(db)
    cfg.max_size_pct = float(risk_limits["max_position_pct"])

    portfolio = broker.snapshot_portfolio()
    positions = portfolio.get("positions", []) or []
    if not positions:
        return {"ok": True, "skipped": "空仓,无点位可算"}
    held = {p["code"] for p in positions}
    # 收盘后 snapshot 的 current_price 即当日收盘(盘外无实时 overlay)。
    closes = {p["code"]: float(p.get("current_price") or 0)
              for p in positions}

    ctx = build_guard_context(db, portfolio, closes, risk_limits, provider)

    def dispatcher(name: str, inp: dict) -> str:
        if name == "inspect_position":
            code = str(inp.get("code", ""))
            pos = next((p for p in positions if p.get("code") == code), None)
            if pos is None:
                return json.dumps({"held": False, "code": code})
            return json.dumps({"held": True,
                               **{k: v for k, v in pos.items()
                                  if isinstance(v, (int, float, str))}},
                              ensure_ascii=False)
        return json.dumps({"error": f"unknown tool {name}"})

    loop = LLMDecisionLoop(llm_client, cfg,
                           terminal_tool="submit_close_plans",
                           system_prompt=CLOSE_REPLAN_SYSTEM,
                           tools=_tools_close_replan(cfg.max_size_pct))
    terminal, reasoning, debug = loop.run(ctx, dispatcher)
    if debug.get("error"):
        return {"ok": False, "error": debug.get("error", "")}
    if debug.get("stop_reason") == "max_tokens" and not terminal:
        return {"ok": False, "error": "输出被 max_tokens 截断"}

    _orders, plans, rejects = validate_decision(
        {"orders": [], "plans": terminal.get("plans") or []},
        held, held, cfg.max_size_pct, cfg.max_orders)
    # 有效点位先落库(部分进展不回滚),再判完整性——缺谁重试谁的成本
    # 由调度层整轮重试承担,灾难地板在重试成功前兜底。
    n_plans, missing = _persist_plans(db, plans, held)

    summary = f"收盘后点位重算: {n_plans}/{len(held)} 持仓已更新"
    if missing:
        summary += f",缺 {len(missing)} 只({'、'.join(missing[:5])})"
    if reasoning:
        summary += f" — {reasoning[:100]}"
    db.log_decision("llm_close_replan", summary, details={
        "plans": plans, "missing": missing, "rejects": rejects,
        "reasoning": reasoning, "usage": debug.get("usage", {}),
    })
    if missing:
        return {"ok": False, "n_plans": n_plans, "missing": missing,
                "error": f"{len(missing)} 只持仓未拿到点位"}
    return {"ok": True, "n_plans": n_plans, "missing": []}
