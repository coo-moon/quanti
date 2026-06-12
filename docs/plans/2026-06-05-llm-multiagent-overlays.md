# LLM 多智能体增强层 — 设计与落地

**Date**: 2026-06-05
**Owner**: wenbo
**Status**: implemented (worktree, 220 tests green)
**一句话**: 借鉴 TradingAgents 的多智能体思路,在 quanti 既有"确定性量化内核 + 单次 LLM 判断"之上,加四个**可选、可叠加、默认全关、逐层降级**的增强层,并支持 DeepSeek / Anthropic 双供应商。

## 背景

quanti 已是 rules-first 系统:走查选股 + 截面因子 + ensemble 融合 + `RiskManager` 硬限 + `PaperBroker`(T+1/涨跌停/费用),LLM 仅在 `agent_mode="llm"` 时对**已 vetted 的候选**做一次有界判断(`llm_runtime.py`)。

对标 [TradingAgents](https://github.com/TauricResearch/TradingAgents)(LLM-first:多空辩论 + 分析师分工 + 风控辩论 + 反思记忆),我们**不搬它的整体架构**(贵/慢/不可回测),只移植它最有价值的几个机制,作为 quanti 现有管线的**加法层**。

## 红线(不可触碰)

1. 确定性内核是地板:回测/选股/因子/`RiskManager`(单股 10% / 行业 30% / 总仓 80% / 回撤 15%)/ T+1 / ST 屏蔽,LLM 一律不能绕过或突破。
2. LLM 只做加法:只能**倾斜排序、增加上下文、缩小仓位**,永远受 候选白名单 + size 校验 + 风控硬限 三重约束。
3. 默认全关;任意环节(无 key / 无网 / 无 SDK / 解析失败)必须**降级到原行为**,绝不向 agent tick 抛异常。
4. 全关时行为与改造前**逐字节一致**(全量回归保证)。

---

## 四个增强层

### ① 新闻情绪 overlay
- **动机**: 整个系统 0 条新闻/情绪/基本面输入,而 A 股高度情绪驱动 —— 这是相对 TradingAgents 唯一真实的内容短板。
- **实现**: 对融合候选的 composite top-N 拉近期新闻(AkShare `stock_news_em`)→ LLM 批量打分 `∈[-1,1]` → 按 `(code, 交易日)` 缓存。作为**第三个 blend 项**接进 `fuse_buy_signals`(与既有 `factor_blend` 同构)。
- **落点**: `quanti/agent/sentiment.py`(新)、`signal_pipeline.fuse_buy_signals`、`runtime._compute_sentiment`、DB 表 `news_sentiment`。
- **作用域**: ensemble 与 llm 两种模式都生效(在候选融合里)。
- **成本**: 每 tick 1 次批量调用,按天缓存;无 LLM 时为 no-op。

### ② 多空辩论(Bull / Bear）
- **动机**: 把单次 LLM 判断升级为对抗性讨论,逼出风险。
- **实现**: 同一份候选上下文,跑 N 轮 Bull→Bear 文本对辩,辩论稿拼进上下文,再由原判断循环(研究主管)`propose_orders`。
- **落点**: `llm_runtime.run_debate` / `BULL_SYSTEM` / `BEAR_SYSTEM`,在 `run_llm_decision` 接入。
- **作用域**: 仅 llm 路。**成本**: 2×轮数 次调用。

### ③ 风控三角(Aggressive / Neutral / Conservative)
- **动机**: 机械 `RiskManager` 是硬地板;再加一层"该不该上、上多大"的判断。
- **实现**: 三个风控人格各给每单 `keep_pct∈[0,1]`(0=否决),按 `risk_tolerance` 聚合(low→min / medium→mean / high→max),`size_pct ×= keep_pct`。`keep_pct≤1` 保证**只能缩仓或否决**,机械硬限仍兜底。
- **落点**: `llm_runtime.run_risk_debate` + `submit_risk_review` 工具,在 `run_llm_decision` 提议后、执行前接入。
- **作用域**: 仅 llm 路。**成本**: 3 次调用。

### ④ outcome-keyed 反思记忆
- **动机**: 把上下文的"最近 N 条决策"升级为"**与当前候选相关、绑定已实现盈亏**的 N 条"。
- **实现**: 只读地从 `trades` 表 FIFO 重建已平仓 round-trip → 算已实现收益(含佣金净额)→ 按相关度检索(同代码 > 同行业)→ 模板化"经验教训"注入上下文。**不动 schema、不挂 broker hook、0 LLM 成本。**
- **落点**: `quanti/agent/reflection.py`(新),在 `build_context_message` 注入。
- **作用域**: 仅 llm 路。**相似度**用分类相似(代码/行业),向量库版留作后续。

### 供应商适配(DeepSeek / Anthropic)
- **动机**: 不强绑 Anthropic;DeepSeek 是 OpenAI 兼容接口,且 quanti 已依赖 httpx。
- **实现**: `quanti/agent/openai_compat.py`(新)用 httpx 实现 `LLMClient` 协议,双向翻译 Anthropic↔OpenAI(system / 多轮 tool_use·tool_result / tools→functions / finish_reason→stop_reason / usage)。单工具调用自动 `tool_choice` 强制;`claude-*` 模型名自动重映射为 `deepseek-chat`。
- **落点**: `runtime._build_llm_client(params)` 按 `llm_provider` 选;四个层全走同一客户端。
- **注意**: 工具流程用 `deepseek-chat`(V3);`deepseek-reasoner`(R1)function calling 支持差。
- **2026-06-11 更新**: DeepSeek 默认模型切换为 `deepseek-v4-pro`(V4 发布后实测:思考模式拒绝强制 `tool_choice`,客户端对单工具强制调用自动附 `thinking: {"type": "disabled"}`;自由文本(辩论/风险角色)保留思考模式)。`deepseek-chat` 别名现由 v4-flash 服务,仍可经 `llm_model` 显式指定。

---

## 完整 tick 流程

```
确定性外层(每 tick): try_fill_pending → resolve_universe → ensure_recent_data → run_screener
   → 分三路: 单策略 / ensemble / LLM(agent_mode="llm")
两路共用候选生成 _compute_fused_candidates:
   Selector top-K → 截面因子 → [①情绪 overlay] → fuse_buy_signals(策略⊕因子⊕情绪) → industry_cap/threshold
LLM 路 run_llm_decision:
   snapshot + [④反思(只读)] → build_context(候选+情绪分+经验) → [②多空辩论] → 研究主管 propose_orders
   → 校验(白名单/≤10%/仅BUY/≤5单) → [③风控三角(只能缩/否决)]
共用执行尾(确定性): check_stop_loss → RiskManager 硬限 → PaperBroker(T+1/涨跌停/费用) → log_decision + 快照
供应商插头: _build_llm_client(params["llm_provider"]) → DeepSeek(httpx) / Anthropic(SDK)
```

---

## goal.params 开关速查

| key | 类型 | 默认 | 作用 | 作用域 |
|-----|------|------|------|--------|
| `agent_mode` | str | `""` | `"llm"` 启用 LLM 决策路 | — |
| `ensemble_enabled` | bool | false | Top-K 策略融合 | rule/ensemble |
| `llm_provider` | str | `"anthropic"` | `"deepseek"` / `"anthropic"` | LLM + 情绪 |
| `llm_model` | str | `claude-sonnet-4-5` | 模型名(deepseek 留空也会自动用 deepseek-chat) | LLM + 情绪 |
| `sentiment_enabled` | bool | false | ① 新闻情绪 overlay | ensemble + llm |
| `sentiment_blend` | float | 0.0 | 情绪在融合中的权重(0~1) | ensemble + llm |
| `sentiment_max_codes` | int | 30 | 每 tick 最多打分股票数(控成本) | ensemble + llm |
| `llm_debate` | bool | false | ② 多空辩论 | llm |
| `llm_debate_rounds` | int | 1 | 辩论轮数 | llm |
| `llm_risk_debate` | bool | false | ③ 风控三角 | llm |
| `llm_reflection` | bool | false | ④ 反思记忆 | llm |
| `llm_max_reflections` | int | 8 | 注入的经验条数上限 | llm |

环境变量: `DEEPSEEK_API_KEY` 或 `ANTHROPIC_API_KEY`(后者还需 `pip install -e '.[llm]'`)。

**全开 + DeepSeek 示例**:
```python
goal.params = {
  "agent_mode": "llm", "llm_provider": "deepseek", "llm_model": "deepseek-chat",
  "ensemble_enabled": True, "top_k_strategies": 3, "factor_blend": 0.4,
  "sentiment_enabled": True, "sentiment_blend": 0.2,
  "llm_debate": True, "llm_debate_rounds": 1,
  "llm_risk_debate": True,
  "llm_reflection": True, "llm_max_reflections": 8,
}
```
此时单 tick LLM 调用 ≈ 情绪1 + 辩论2 + 经理1 + 风控3 = 约 7 次(④ 加 0)。

## 暴露面

- **MCP** (`quanti/mcp_server.py`): `set_goal` 的 `params` 已是直通对象;本次把上述 key 写进它的 inputSchema(`additionalProperties: true` 保留扩展性),让 OpenClaw / Claude Desktop 等客户端可发现这些开关。`get_goal` 回显 params。
- **Web** (`web/src/views/Agent.vue`): "Agent 模式"卡片下新增"LLM 增强层"区:供应商下拉 + 情绪/辩论/风控/反思开关与数值,沿用既有 `advParams ↔ goalDraft.params` 双向同步;保存目标即落库。
- **决策日志**: `llm_cycle` details 含 `debate_rounds` / `risk_review` / `n_reflections`;`sentiment_overlay` 为独立 kind。

## 测试

- 单测(全程 stub LLM,无网):`test_sentiment_overlay.py`、`test_debate.py`、`test_risk_debate.py`、`test_reflection.py`、`test_openai_compat.py`(httpx MockTransport 验证翻译 + e2e)。
- 实采验证:`fetch_recent_news` 对真实 AkShare `stock_news_em` 列名(新闻标题/发布时间/文章来源)逐一命中、时效过滤与降级正确。
- 手动 smoke:`scripts/smoke_llm_sentiment.py`(真新闻 → 真 LLM 打分 + 多空 sign sanity;自动按 key 选供应商)。
- 全量 **220 passed**,新文件 ruff 干净,全关时无回归。

## 文件地图

| 文件 | 说明 |
|------|------|
| `quanti/agent/sentiment.py` 🆕 | ① 新闻分析师(拉新闻 + 批量打分 + 缓存) |
| `quanti/agent/reflection.py` 🆕 | ④ FIFO 已实现收益 + 相关度检索 |
| `quanti/agent/openai_compat.py` 🆕 | DeepSeek/OpenAI 兼容客户端(httpx) |
| `quanti/agent/llm_runtime.py` | ②③ + 反思上下文 + LLMConfig 开关 |
| `quanti/agent/signal_pipeline.py` | `fuse_buy_signals` 三路融合 + `FusedCandidate.sentiment_score` |
| `quanti/agent/runtime.py` | `_compute_sentiment` / `_build_llm_client` / 参数接线 |
| `quanti/data/database.py` | `news_sentiment` 表 + get/upsert |
| `quanti/mcp_server.py` | `set_goal` params schema 文档化 |
| `web/src/views/Agent.vue` | LLM 增强层 UI |
| `scripts/smoke_llm_sentiment.py` 🆕 | 真 LLM 手动 smoke |

## 后续(未做)

- 真·LLM 实跑验证(需 key):`scripts/smoke_llm_sentiment.py`。
- 反思记忆升级为 embedding 相似度检索(当前为代码/行业分类相似)。
- 风控三角可压成"1 次调用返回三视角"以降成本(当前 3 次,保真度优先)。
- 决策日志面板渲染辩论稿 / 风控 keep_pct / 命中的历史经验(目前仅 `reasoning`)。
- 情绪源扩展(研报、龙虎榜、公告)与基本面分析师。
