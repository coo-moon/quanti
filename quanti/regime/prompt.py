"""把已落库的 regime 快照变成 agent 能用的两样东西:tick 日志行 + LLM 上下文段。

**只读,永不现算。** tick 全程持 `_broker_lock`(runtime.py `_run_one_cycle`),
而盘中止损/熔断的 `_intraday_guard` 抢的是同一把锁 —— 生产日志里已经出现过
9 分 09 秒的止损空窗。在 tick 里跑 `report.generate()`(全市场扫描 10s +
LLM 最长 300s)会把这个空窗拉到 14 分钟以上。这里只做一次 `load_latest`
(实测 0.9ms),快照由 17:30 的后台任务负责生产。

**三道闸**(任何一道不过都返回空,tick 照常跑):

1. `goal.params["regime_in_prompt"]` — **默认开**(2026-07-30 起,口径同
   `wf_enabled`:键缺失=开,只有显式 `false` 才关)。默认开的前提是本模块把
   注入内容压到了「客观数字 + 禁令」这一层(见下),而不是快照里那半边 LLM
   仓位建议;真要关就在 Web UI「高级开关」里取消勾选,会显式写 `false` 落库。
2. **成交模式**:只有 next-open 的 `fill_mode="pending"` 才注入。CLI /
   MCP 的 `agent tick` 走 `immediate`,同 bar 以当日 close 成交 —— 拿 T 日
   收盘算出来的全市场宽度去影响 T 日 close 的成交,是教科书级前视。
3. **陈旧闸**:快照与决策日相隔超过 1 个交易日就不注入(长假、后台任务挂了、
   数据没同步上)。宁可没有环境描述,也不要拿上周的宽度当今天的。

**注入什么、不注入什么**——这是本模块最重要的部分,别随手加字段:

只注入**客观数字**(宽度、涨跌家数、大盘 vs 等权、成交额、规则层投票标签)。
明确剔除快照里 LLM 生成的那一半 —— `action`("加仓/持仓/减仓/观望")、
`headline`、`llm_regime`、`drivers`、`risk_notes`、`sectors_favored/avoid`。
理由是实测的,不是洁癖:

* `action` 是另一个 LLM 的仓位指令。裁判 LLM 唯一能做的动作是 `propose_orders`,
  它表达「观望」的唯一方式就是少下单或砍 `size_pct` —— 而本项目的定论是
  收益大头来自选池 beta,照抄 action 等于直接砍掉赚钱的那部分。
* 板块推荐有**负** alpha:行业 20 日动量 → 未来 20 日行业收益,日均横截面
  rank IC -0.0725(t=-9.27,1035 个交易日重放),且与生产的
  `industry_neutral=true`(横截面行业去均值)正面对冲。
* 规则层标签本身前瞻方向也是负的(「上涨」桶未来 20 日收益 ≤ 基线,
  t=-2.30)。所以它进 prompt 的身份是**环境描述**,不是信号 —— 段末那句
  禁令必须一起注入,否则就是把一个被自己回测否定的择时信号喂给决策者。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

#: 快照与决策日最多相隔几个交易日。1 = 「昨天收盘的」可用,更早的不要。
MAX_STALE_TRADING_DAYS = 1

PARAM = "regime_in_prompt"

#: tick 第一步的观测日志开关(runtime.py)。放在这里而不是 runtime,是因为
#: 「默认开」的口径必须和 PARAM 一致 —— 两处各写一遍 `.get(k, True)` 早晚漂移。
DETECT_PARAM = "regime_detect"


def enabled(goal, param: str = PARAM) -> bool:
    """键缺失即开(同 `wf_enabled`),只有显式 falsy 才关。

    默认开意味着**已落库但没有这个键的 goal 会在下一 tick 自动开始生效**,
    不需要用户去 UI 点一次保存 —— 这正是「默认开」的语义。要关就在 UI 取消
    勾选,`syncParamsFromAdv` 会显式写 `false`。
    """
    return bool((getattr(goal, "params", None) or {}).get(param, True))


def fill_mode_ok(broker) -> bool:
    """只有 next-open 成交才安全。PaperBroker 有 `_fill_mode`;实盘 QmtBroker
    没有这个属性(它走券商真实撮合,不存在同 bar close 成交),默认放行。

    公开的,因为 tick 的日志行也要用它 —— 否则日志会说「已注入」而实际上被
    这道闸拦掉了。"""
    return getattr(broker, "_fill_mode", "pending") == "pending"


def latest_usable(db, *, provider=None, now: datetime | None = None
                  ) -> tuple[dict | None, str]:
    """→ (快照, 不可用原因)。原因为空串表示可用。

    tick 日志和 prompt 注入共用这一个判断,免得两处口径漂移。
    """
    from quanti.regime import report  # noqa: PLC0415 - 懒加载,避免 import 环
    from quanti.utils.market import (  # noqa: PLC0415
        count_trading_days_between, order_decision_date)

    snap = report.load_latest(db)
    if not snap:
        return None, "无快照"
    try:
        snap_d = date.fromisoformat(str(snap["date"]))
    except (ValueError, KeyError, TypeError):
        return None, f"快照日期不可解析: {snap.get('date')!r}"
    decision_d = order_decision_date(now or datetime.now(), provider)
    if snap_d > decision_d:
        # 方向闸。count_trading_days_between 在 start >= end 时返回 0(见
        # utils/market.py),所以「快照比决策日还新」会被陈旧闸读成「0 个交易日,
        # 很新鲜」直接放行 —— 而 load_latest 拿的是库里最新一行、不带 as_of。
        # 今天没有历史回放调用者能踩到,但默认开之后,下一个写 LLM 回放的人
        # 继承到的是「开」,那就是拿今天的全市场宽度去影响去年的决策。
        return snap, f"快照晚于决策日({snap_d} > {decision_d})"
    stale = count_trading_days_between(snap_d, decision_d, provider)
    if stale > MAX_STALE_TRADING_DAYS:
        return snap, f"快照陈旧({snap_d} 距决策日 {decision_d} 已 {stale} 个交易日)"
    return snap, ""


def _fmt(v, digits=1, unit="%") -> str:
    return "—" if v is None else f"{float(v):.{digits}f}{unit}"


def _signed(v, digits=2) -> str:
    return "—" if v is None else f"{float(v):+.{digits}f}%"


def render_block(snap: dict) -> str:
    """快照 → 注入裁判 LLM 的上下文段。只含客观数字 + 规则层标签 + 禁令。"""
    m = snap.get("metrics") or {}
    n = m.get("n_stocks")
    # 只写快照日期,不写「上一交易日」——tick 跑在 17:30 之前读到的是 T-1、
    # 之后读到的是 T,写死任一个都会有一半时间是错的。日期本身已说明一切。
    lines = [
        f"\n\n# 市场环境(截至 {snap['date']} 收盘"
        + (f", 全市场 {int(n)} 只" if n else "") + ")",
        f"- 站上 MA20/50/200: {_fmt(m.get('above20'))} / "
        f"{_fmt(m.get('above50'))} / {_fmt(m.get('above200'))}",
    ]
    if m.get("up") is not None:
        ad = m.get("ad_ratio")
        lines.append(
            f"- 涨/跌家数: {int(m['up'])}/{int(m.get('dn') or 0)}"
            + (f"(涨跌比 {float(ad):.2f})" if ad is not None else ""))
    if m.get("nh") is not None:
        lines.append(f"- 20日新高/新低: {int(m['nh'])}/{int(m.get('nl') or 0)}")
    lines.append(f"- 近5日 大盘(市值加权) {_signed(m.get('cap5'))} / "
                 f"等权 {_signed(m.get('eq5'))}")
    lines.append(f"- 近20日 大盘 {_signed(m.get('cap20'))} / "
                 f"等权 {_signed(m.get('eq20'))}")
    if m.get("amt_chg") is not None:
        lines.append(f"- 成交额 5日均 vs 20日均: {_signed(m.get('amt_chg'), 1)}")
    lines.append(f"- 规则层判定: {snap.get('rule_label', '—')}"
                 f"(投票分 {int(snap.get('rule_score') or 0):+d})")
    # 这段禁令和数字是一个整体,不要分开注入。宽度是滞后指标,本系统全市场
    # 重放已证它对未来 20 日收益的信息量在 0 到负之间;它的用途是让你理解
    # 单只票所处的环境,不是让你据此决定今天下几单。
    # 措辞刻意避开「加仓/减仓/观望」这三个词:tests 用字符串黑名单确保快照里
    # LLM 写的仓位建议没有漏进来,禁令自己用到这些词会让那道防线失灵。
    lines.append(
        "\n注:以上仅为市场环境描述,供你理解候选个股所处的大环境。"
        "本系统全市场回测已证 regime 择时无 alpha(宽度是滞后指标,"
        "与未来 20 日收益的相关性在 0 到负之间)。"
        "**不得据此调整 size_pct、不得因此少下单或空仓等待**;"
        "选股与仓位仍以候选的 final_score 和风控限额为准。")
    return "\n".join(lines)


def regime_block(db, goal, broker, *, provider=None,
                 now: datetime | None = None) -> tuple[str, dict]:
    """→ (注入文本, 元信息)。任何一道闸不过 / 任何异常都给空串。

    元信息用于把「这一 tick 到底注没注、注的哪天」写进决策日志 —— 没有它,
    三年后连「当时注了没有」都分辨不出来。
    """
    meta: dict = {"regime_injected": False, "regime_snapshot_date": None,
                  "regime_skip_reason": ""}
    if not enabled(goal):
        meta["regime_skip_reason"] = "已显式关闭"
        return "", meta
    if not fill_mode_ok(broker):
        # 同 bar close 成交下注入 = 用 T 日收盘信息影响 T 日成交价。
        meta["regime_skip_reason"] = "immediate 成交模式(同 bar 前视)"
        return "", meta
    try:
        snap, reason = latest_usable(db, provider=provider, now=now)
        if reason or not snap:
            meta["regime_skip_reason"] = reason or "无快照"
            if snap:
                meta["regime_snapshot_date"] = snap.get("date")
            return "", meta
        meta.update(regime_injected=True, regime_snapshot_date=snap.get("date"))
        return render_block(snap), meta
    except Exception as e:  # noqa: BLE001 - 上下文缺一段远好过打掉一个 tick
        logger.warning("regime block skipped: %s", e)
        meta["regime_skip_reason"] = f"异常: {e}"
        return "", meta
