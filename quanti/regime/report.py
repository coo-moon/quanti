"""每日市场 regime 快照:宽度数据 + 时事面 → DeepSeek 深度思考 → 持久化。

链路(每天 17:30 由 background_sync 触发一次,收盘后行情已落库):

    breadth.build()      全A股宽度/轮动/资金指标(纯 SQL+pandas,规则判定)
    news.fetch_news()    新闻联播 + 财经快讯(尽力而为)
    → LLM(deepseek-v4-flash, thinking 全开) → 结构化 JSON + markdown 正文
    → market.regime_snapshots(每日一行,可回溯)

**为什么不用 tool/function calling 拿结构化输出**:v4 thinking 模式在
*强制* tool_choice 下直接 400(见 openai_compat 的注释),现有 client 遇到
单 tool 会自动 `thinking: disabled` —— 那正好废掉用户要的「最高思考级别」。
所以这里走自由文本,让模型先吐一个 ```json 块再写正文,解析失败也只是丢
结构化字段、正文照存。实测 2026-07-29(当时用 v4-pro):v4 系默认就在
thinking 模式(返回 reasoning_content),`reasoning_effort` / `thinking.effort`
这类分级参数接受但无可观测差异——即默认档就是最高档,无需额外参数。

判定分两层且**都存**:规则层(breadth 的多因子投票)是可复现的锚,LLM 层
是解释与建议。两者背离本身就是有用信号,所以 UI 同时展示,而不是让 LLM
覆盖规则判定。
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime

from quanti.regime import breadth, news as news_mod

logger = logging.getLogger(__name__)

MODEL = "deepseek-v4-flash"
MAX_TOKENS = 8000
LLM_TIMEOUT = 300.0

#: 报告要给出的动作档位。固定枚举,UI 才好上色/对比,也防 LLM 自由发挥出
#: 「满仓梭哈」这种没法审计的词。
ACTIONS = ("加仓", "持仓", "减仓", "观望")
REGIMES = ("上涨", "震荡", "下跌")

SYSTEM = """你是一位 A 股市场结构分析师,为一个量化交易系统撰写每日 regime 快照。

你的读者是系统的操盘人,他已经知道所有指标定义,不需要科普。他要的是:
今天市场处于什么状态、为什么、以及这对仓位意味着什么。

必须遵守的前提(这个系统已用全市场数据反复验证过,别给出与之冲突的建议):
- 技术择时、regime 择时在本项目的严格回测中**均无 alpha**;均线类护栏的
  价值只是降回撤,不是选时赚钱。所以不要写「现在满仓抄底」这类方向性豪赌。
- 收益大头来自选池 beta 而非池内 alpha;被动等权基线常常跑赢花哨策略。
- 等权/小盘口径的漂亮回测数字往往是 size beta 幻觉,市值加权口径才可交易。
- 你不是持牌投资顾问,不提供个性化投资建议;给的是市场状态判断与仓位框架。

写作要求:
- 中文,直给结论,不铺垫、不复述题目、不写「综上所述」。
- 每个判断都要挂靠给你的具体数字或新闻,不许凭空断言。
- 数据面与消息面冲突时明确指出冲突,不要和稀泥。
- 若新闻源标注「未取到」,就直说消息面缺失,严禁编造政策或事件。"""

PROMPT_TMPL = """今天是 {today}(数据截止 {latest})。下面是全 A 股({n_stocks} 只)的当日市场结构数据与时事面材料。

请先输出一个 ```json 代码块,严格按下面的字段:

```json
{{
  "regime": "上涨|震荡|下跌 三选一",
  "confidence": 0-100 的整数,你对该判定的把握,
  "headline": "一句话结论,不超过 30 字",
  "drivers": ["支撑该判定的 3-5 条依据,每条挂靠具体数字或新闻"],
  "sectors_favored": ["数据上占优的 2-4 个板块"],
  "sectors_avoid": ["数据上明显走弱、暂应回避的 2-4 个板块"],
  "action": "{actions} 四选一",
  "risk_notes": ["2-4 条风险/证伪点,即什么情况下该判定会被推翻"]
}}
```

然后另起一行写 markdown 正文报告,包含:趋势判定与依据、大盘 vs 小盘背离、
板块轮动、资金面、时事政治面解读、以及今天的仓位建议(含明确的观察窗口/
证伪触发点)。正文控制在 800-1200 字。

---

## 市场结构数据

{breadth_md}

## 规则层判定(多因子投票,供你参考,可以不同意但要说明理由)

{rule_label}(投票分 {rule_score:+d}) — {rule_reasons}

---

## 时事面材料

{news_md}
"""


# ------------------------------------------------------------------ LLM

def _extract_json(text: str) -> dict:
    """从报告里抠出第一个 json 块。抠不到返回 {} —— 正文仍然有价值。"""
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        m = re.search(r"(\{[^{}]*\"regime\".*?\})", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.warning("regime LLM json block unparseable: %s", e)
        return {}


def _strip_json_block(text: str) -> str:
    """正文 = 去掉 json 块后的部分。"""
    return re.sub(r"```json\s*\{.*?\}\s*```", "", text, count=1, flags=re.S).strip()


def _normalize(parsed: dict) -> dict:
    """把 LLM 的自由发挥收敛回固定枚举,UI 才能安全上色。"""
    out = dict(parsed)
    reg = str(parsed.get("regime", "")).strip()
    out["regime"] = next((r for r in REGIMES if r in reg), "")
    act = str(parsed.get("action", "")).strip()
    out["action"] = next((a for a in ACTIONS if a in act), "")
    try:
        c = int(float(parsed.get("confidence", 0)))
    except (TypeError, ValueError):
        c = 0
    out["confidence"] = max(0, min(100, c))
    for k in ("drivers", "sectors_favored", "sectors_avoid", "risk_notes"):
        v = parsed.get(k) or []
        out[k] = [str(x) for x in v] if isinstance(v, list) else [str(v)]
    out["headline"] = str(parsed.get("headline", "")).strip()
    return out


def build_prompt(r: dict, news: dict) -> str:
    return PROMPT_TMPL.format(
        today=date.today().isoformat(), latest=r["latest"],
        n_stocks=r["n_stocks"], actions="|".join(ACTIONS),
        breadth_md=breadth.render(r),
        rule_label=r["label"], rule_score=r["score"],
        rule_reasons="；".join(r["reasons"]),
        news_md=news_mod.render_news(news),
    )


def run_llm(prompt: str, llm=None) -> tuple[str, dict, str]:
    """→ (正文 markdown, 结构化 dict, 实际模型名)。

    `llm` 可注入(测试用假 client);默认构造 DeepSeekLLMClient。
    **不传 tools** —— 这是保住 thinking 模式的关键,见模块 docstring。
    """
    if llm is None:
        from quanti.agent.openai_compat import DeepSeekLLMClient  # noqa: PLC0415
        # 默认 60s 对 thinking 模式远远不够:一次深度思考 + 千字报告实测
        # 90-180s,60s 会稳定超时把每天的快照打成 LLM-failed。
        llm = DeepSeekLLMClient(default_model=MODEL, timeout=LLM_TIMEOUT)
    resp = llm.create_message(
        model=MODEL,
        system=[{"type": "text", "text": SYSTEM}],
        messages=[{"role": "user", "content": prompt}],
        tools=None,
        max_tokens=MAX_TOKENS,
        temperature=0.4,
    )
    text = "\n".join(b.get("text", "") for b in resp.get("content", [])
                     if b.get("type") == "text").strip()
    if not text:
        raise RuntimeError(f"LLM 返回空正文 (stop_reason={resp.get('stop_reason')})")
    parsed = _normalize(_extract_json(text))
    return _strip_json_block(text), parsed, MODEL


# ------------------------------------------------------------------ 持久化

def _json_safe(obj):
    """递归把非有限浮点(NaN/±Inf)换成 None,其余原样返回。

    裸 `NaN`/`Infinity` **不是合法 JSON**,但 `json.dumps` 默认 allow_nan=True
    照写不误,于是非法 JSON 进了库;`json.loads` 默认又能把它读回 float('nan'),
    所以本地读写都「看着正常」,直到 FastAPI/starlette 用 allow_nan=False 序列化
    响应 —— 整条 /api/regime/* 直接 500(2026-08-06 真实发生:那天全市场只同步到
    1 只股票,breadth 对空切片求 mean/median 得 NaN)。前端 `JSON.parse` 同样吃
    不下裸 NaN。

    指标算不出来的语义就是「无值」,对应 JSON null —— 前端 num() 已按 null 显示
    「—」,所以这里统一收敛到 None,而不是丢字段或填 0(填 0 会被读成「真的是 0%」)。

    写侧(save)与读侧(_row_to_dict)都过一遍:写侧保证新数据干净,读侧兜住修复
    前已落库的污染行(否则历史接口永远 500,除非手工改库)。
    """
    if isinstance(obj, float):        # np.float64 是 float 子类,一并覆盖
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _sectors_payload(r: dict) -> dict:
    """板块榜 DataFrame → 可 JSON 化的三张表。"""
    def rows(df):
        return [{"industry": str(i), "ret": round(float(row["mean"]), 2),
                 "n": int(row["count"])} for i, row in df.iterrows()]
    return {"top20": rows(r["ind_top"]), "bottom20": rows(r["ind_bot"]),
            "top5d": rows(r["ind5_top"])}


def _metrics_payload(r: dict) -> dict:
    keys = ("above20", "above50", "above200", "cap1", "eq1", "cap5", "eq5",
            "cap20", "eq20", "up", "dn", "fl", "ad_ratio", "nh", "nl",
            "amt_today", "amt5", "amt20", "amt_chg", "turn", "n_stocks")
    return {k: (round(v, 3) if isinstance(v, float) else v)
            for k, v in ((k, r.get(k)) for k in keys) if v is not None}


def save(db, snap: dict) -> None:
    """UPSERT 当日快照。同一天重跑覆盖,不堆重复行。"""
    db.conn.execute(
        """INSERT INTO regime_snapshots
           (date, rule_label, rule_score, llm_regime, llm_confidence, headline,
            action, metrics_json, sectors_json, llm_json, report_md, news_json,
            model, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET
             rule_label=excluded.rule_label, rule_score=excluded.rule_score,
             llm_regime=excluded.llm_regime, llm_confidence=excluded.llm_confidence,
             headline=excluded.headline, action=excluded.action,
             metrics_json=excluded.metrics_json, sectors_json=excluded.sectors_json,
             llm_json=excluded.llm_json, report_md=excluded.report_md,
             news_json=excluded.news_json, model=excluded.model,
             created_at=excluded.created_at""",
        (snap["date"], snap["rule_label"], snap["rule_score"],
         snap["llm_regime"], snap["llm_confidence"], snap["headline"],
         snap["action"],
         # 每个 JSON 列都过 _json_safe:NaN/Inf 绝不能进库(非法 JSON)。
         # llm/news 也过 —— LLM 吐的 json 块里同样可能带裸 NaN,
         # _extract_json 的 json.loads 默认会照单全收。
         json.dumps(_json_safe(snap["metrics"]), ensure_ascii=False),
         json.dumps(_json_safe(snap["sectors"]), ensure_ascii=False),
         json.dumps(_json_safe(snap["llm"]), ensure_ascii=False),
         snap["report_md"],
         json.dumps(_json_safe(snap["news"]), ensure_ascii=False), snap["model"],
         snap["created_at"]))
    db.conn.commit()


def _row_to_dict(row, *, full: bool) -> dict:
    # 读侧净化:修复前落库的行仍带裸 NaN,json.loads 会把它读回 float('nan'),
    # 直接返回就会让 /api/regime/* 在响应序列化时 500。过一遍 _json_safe,
    # 老数据无需迁移即可正常读出(NaN → null)。
    d = {"date": row[0], "rule_label": row[1], "rule_score": row[2],
         "llm_regime": row[3], "llm_confidence": row[4], "headline": row[5],
         "action": row[6], "metrics": _json_safe(json.loads(row[7] or "{}")),
         "model": row[12], "created_at": row[13]}
    if full:
        d.update(sectors=_json_safe(json.loads(row[8] or "{}")),
                 llm=_json_safe(json.loads(row[9] or "{}")),
                 report_md=row[10] or "",
                 news=_json_safe(json.loads(row[11] or "{}")))
    return d


_COLS = ("date, rule_label, rule_score, llm_regime, llm_confidence, headline, "
         "action, metrics_json, sectors_json, llm_json, report_md, news_json, "
         "model, created_at")


def load_latest(db) -> dict | None:
    row = db.conn.execute(
        f"SELECT {_COLS} FROM regime_snapshots ORDER BY date DESC LIMIT 1"
    ).fetchone()
    return _row_to_dict(row, full=True) if row else None


def load_one(db, day: str) -> dict | None:
    row = db.conn.execute(
        f"SELECT {_COLS} FROM regime_snapshots WHERE date=?", (day,)).fetchone()
    return _row_to_dict(row, full=True) if row else None


def load_history(db, limit: int = 90) -> list[dict]:
    """历史列表:不带正文/新闻(体积大),够画时间轴和列表。"""
    rows = db.conn.execute(
        f"SELECT {_COLS} FROM regime_snapshots ORDER BY date DESC LIMIT ?",
        (limit,)).fetchall()
    return [_row_to_dict(r, full=False) for r in rows]


# ------------------------------------------------------------------ 入口

def generate(db, db_path: str | None = None, llm=None,
             with_news: bool = True) -> dict:
    """跑一次完整快照并落库,返回快照 dict。

    LLM 失败不代表整次失败:宽度指标是确定性的、当天唯一,照样存下来(LLM
    字段留空),UI 至少还有规则层判定和全部指标 —— 比整天开天窗强。
    """
    path = db_path or getattr(db, "market_db_path", None) or breadth.DB
    r = breadth.build(path)
    news = news_mod.fetch_news() if with_news else {"cctv": [], "flash": []}
    report_md, parsed, model = "", {}, ""
    try:
        report_md, parsed, model = run_llm(build_prompt(r, news), llm=llm)
    except Exception as e:  # noqa: BLE001 - 数据面永远要落库
        logger.warning("regime LLM failed, saving data-only snapshot: %s", e)
        report_md = f"(LLM 报告生成失败:{e};以下仅数据面)\n\n" + breadth.render(r)
    snap = {
        "date": r["latest"],
        "rule_label": r["label"], "rule_score": int(r["score"]),
        "llm_regime": parsed.get("regime", ""),
        "llm_confidence": int(parsed.get("confidence", 0) or 0),
        "headline": parsed.get("headline", ""),
        "action": parsed.get("action", ""),
        "metrics": _metrics_payload(r), "sectors": _sectors_payload(r),
        "llm": parsed, "report_md": report_md, "news": news,
        "model": model, "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    save(db, snap)
    logger.info("regime snapshot saved: %s rule=%s llm=%s",
                snap["date"], snap["rule_label"], snap["llm_regime"] or "-")
    return snap
