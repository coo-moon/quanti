# quanti 实盘就绪审计报告

**日期**：2026-06-22
**范围**：面向"接入 QMT 真实券商实盘"的全链路正确性审计。覆盖 12 个分桶：回测引擎、纸面盘 broker、QMT broker/bridge、风控、selector/walk-forward/hyperopt、runtime/信号/票池、数据适配器（akshare/xtdata/tushare）、因子 DSL/挖掘、metrics、测试覆盖、交易日历/复权、单位口径。
**方法**：四维度（数据正确性 / 逻辑错误 / 回测正确性 / 无过拟合）逐桶通读 + 对抗式双视角验证（视角 A 核实机制属实，视角 B 质疑危害是否真实/被夸大），每条结论附 `file:line` 证据并核对是否已被历史修复 commit 解决。同时整合独立"业界对标 agent"的对标建议。

## 一句话结论

> ✅ **更新(2026-07-08,PR #125–#134)**:本报告列出的 4 个 HIGH 拦路项(C1 回测组合熔断 / C5 实盘现价+逐票止损 / C2+F3 统一 sizing / A1+A2 复权口径)以及 C3/C6/F1/G1/G2/G4 等**均已在代码中修复并补回归测试**(全量 809 passed)。接实盘后端已从 vnpy_backend 换为**直连 xtquant 的 `XtDirectBackend`**(mode=xt),并对江海证券 QMT 实盘完成**只读**端到端验证。上真钱现仅差:①真机下单冒烟(已计划);②业务跑道(模拟盘连续 ≥60 交易日 + 风控触发)——均非本审计的代码项。**本报告结论已过时,仅作历史记录;实盘现状以 [`live-trading-runbook.md`](live-trading-runbook.md) 为准。** 下方正文条目未逐条回改,请以本注记为准。

**当前不建议接入真实 QMT 实盘。**〔历史结论〕 存在 4 个 HIGH 级拦路项必须先修：①回测引擎完全不应用 -15% 组合回撤熔断（risk-01）；②实盘 QMT 持仓现价用成本价导致逐票止损/移动止盈永久失效（qmt-01）；③回测/实盘建仓 sizing 不一致——回测无视 signal.strength（backtest-02 / runtime-01）；④回测源 akshare(qfq) 与实盘源 xtdata(none) 复权口径不一致写入同一张表（qmt-02 / D1+D2）。前两项是会直接导致实盘资金损失或风控失效的安全红线，后两项使"回测验证过的策略 ≠ 实盘表现"。在这 4 项修复并补回归测试前，回测结论不可作为上实盘依据。

> 说明：QMT 实盘通道目前尚未在任何生产路径被实例化（生产 broker 均为 PaperBroker），故 qmt-* 类问题属"接线即触发"的待激活风险，而非今日正在亏钱——但它们正是本次"接实盘前"审计的核心拦路项，必须在接线前修好。

---

## 二、确认问题（按 category 分组，组内按 severity 排序）

### A. 数据正确性 (data-correctness)

#### A1【HIGH】回测源(akshare qfq) 与实盘源(xtdata none) 复权口径不一致，写入同一 daily_quotes 表
- **✅ 状态(2026-06-22)：已修复(Qlib 式)** — `daily_quotes` 改存**原始价 + `adj_factor`**(=hfq/raw),akshare/tushare/xtdata 三源统一原始价口径;`DataProvider` 读时复权(默认 hfq),实盘下单/图表用 `adjust="none"`。升级后须跑一次 `quanti sync --quotes --refetch`。
- **位置**：`quanti/data/akshare_adapter.py:163,206`（adjust="qfq"）；`bridge/vnpy_backend.py:259-274`（get_market_data_ex / download_history_data 均未传 dividend_type → xtdata 默认 'none' 原始价）；`quanti/data/xtdata_adapter.py:67-83`（同一 save_daily_quotes 出口）；`quanti/data/database.py:228,530-538`（主键 (code,date)，INSERT OR REPLACE，无 source 列）
- **问题**：AkShare 两条取数路径均前复权写入；xtdata 经 bridge 取原始未复权价，写入同一张表同一列。增量同步按 (code,date) 用不复权 bar 覆盖此前前复权 bar，在每个除权除息点产生成倍/跳变价格断点。
- **实盘影响**：实盘期切换/混入 xtdata 后，历史价序列在除权点不连续，污染所有价格型指标与回测；同一票切换数据源前后口径不一致 → 回测≢实盘，基于 pnl_pct/价格的止损与选股阈值被系统性扭曲。计划文档（`docs/plans/2026-06-16-live-trading-qmt.md:62,68`）明确宣称"复权由 SDK 处理、避免口径不一致"，而代码不传 dividend_type 使该声明为假——属误导性设计声明。
- **修法**：`vnpy_backend.kline/download_history_data` 显式传与 akshare 一致的复权口径（`dividend_type='front'`），或全链改用"原始价 + 独立复权因子表"。补两源同一除权票收盘口径对齐断言；同步按 code 维度避免混写。
- **业界对照**：Qlib 存"原始价 + 复权因子"读取时按需复权；zipline 用独立 adjustments 表。复权因子单调可增量，不回改历史。

#### A2【HIGH，与 A1 同根因互补】qfq 前复权增量同步存在拼接断层 — 回测历史价随每次同步漂移
- **✅ 状态(2026-06-22)：已修复** — `adj_factor` 锚定上市首日(后复权/原始),同一日期因子永不随同步窗口变化 → 增量追加新行即正确、老行无需重写、可复现。
- **位置**：`quanti/data/akshare_adapter.py:163,206,326`；`quanti/data/background_sync.py:481`；`quanti/data/database.py:532`
- **问题**：前复权特性是整段历史相对"最新一天"重算，分红/送转后过去所有 bar 复权价都会变；而增量同步只拉"最新存储日期往后"的新 bar 并 INSERT OR REPLACE。结果老 bar 保留旧基准 qfq、新 bar 用新基准 qfq，每个除权日拼接点价格跳变。
- **实盘影响**：回测收益率在除权点被人为放大/缩小，且不可复现（取决于上次同步时间）——直接污染回测正确性。
- **修法**：改存后复权 hfq（以上市首日为基准，历史值永不回改，天然支持增量），回测内部用 hfq 算收益率，仅 UI 换算；或检测到 split/dividend 后全量重拉该标的。

#### A3【LOW】akshare(成交量 手) 与 xtdata(volume 股) 单位不一致写入同一 volume 列（tushare 同为手）
- **位置**：`quanti/data/akshare_adapter.py:174,226`（手）；`bridge/vnpy_backend.py:273` / `xtdata_adapter.py:79`（股）；`quanti/data/tushare_adapter.py:175`（手）；`quanti/data/database.py:225`（无单位标注、无 source 列）
- **问题**：三源写同一 volume 列，单位差 100 倍。当前下游 ADV/流动性读 amount(元，两源一致) 而非 volume，故决策结果暂不受污染。
- **实盘影响**：潜在地雷——volume 已作为可用因子字段暴露（`quanti/factors/expr.py:19`、`parser.py:28`、`factor_miner.py:27` 告知 LLM volume 可用）。一旦挖掘出引用 Volume() 且非尺度不变的因子，回测(手)与实盘(股)间出现 100 倍口径跳变，破坏 backtest≡live。多数比值/截面标准化因子尺度不变可兜底，但跨源切换点的 100 倍阶跃断点不受兜底。
- **修法**：写库前统一为股（akshare/tushare ×100），schema/文档标注单位或加 source 列；统一前可在因子层拒绝 Volume()。

#### A4【LOW】交易日历未被回测引擎使用 — 用"数据里出现过的日期"当日历
- **位置**：`quanti/backtest/engine.py:148,150`；`quanti/data/database.py:639`（trade_calendar 表存在却未被回测使用）
- **问题**：回测日历=本批标的数据里出现过日期之并集。若某天全部标的停牌/缺数据，该天不在日历，T+1/TTL/protections 的交易日距离会算错。
- **修法**：回测引擎注入 trade_calendar 作为主时钟，行情缺失当天仍是一个 tick（标的当天 untradeable）。

### B. 回测正确性 (backtest-correctness)

#### B1【MEDIUM】回测下单无"单 bar 可成交量/参与率"容量约束，大额单整笔一次成交，乐观偏差
- **位置**：`quanti/backtest/engine.py:370`（买 qty=min(affordable,10000)）、`:442`（卖 qty=pos.quantity 整笔）；`quanti/backtest/slippage.py:97`（冲击封顶 max_bps=300）
- **问题**：买卖整笔假设在次日开盘一次成交，唯一代价是 VolumeImpactSlippage 价格冲击且被封顶 3%。无"单 bar 最多吃当日 amount/ADV 某比例"的成交量上限。卖出端完全无量上限（整仓一次出）。pending TTL 机制只处理停牌/涨跌停/现金不足，不承接"因量不足而拆单"。
- **实盘影响**：低流动性/小盘/大账户策略回测假设瞬时全仓进出，实盘必分批、滑点更大或成交不全 → 回测收益/胜率偏乐观，上线劣化。ADV 已正确预计算且滑点随参与率 sqrt 递增是部分兜底，故 MEDIUM 而非 HIGH。
- **修法**：单 bar 可成交量 = min(目标量, ADV×参与率上限(10-25%)/价格)，未成交留待下一 bar（衔接现有 pending）。
- **业界对照**：zipline VolumeShareSlippage 默认限制单 bar ≤当日成交量 2.5%。

#### B2【MEDIUM】短 OOS fold 的 oos_annual_return 是未年化累计收益，却被当年化收益除以年化目标，单位错配
- **位置**：`quanti/backtest/metrics.py:12,25-28`；`quanti/agent/walk_forward.py:113(test_days=21),163,173,185`；`quanti/agent/selector.py:258,270-271`
- **问题**：`_MIN_DAYS_TO_ANNUALIZE=60`，默认 wf_test_days=21 日历日（≈15 交易日 <60），故 compute_metrics 返回 annual_return=total_return（未年化累计）。walk_forward 取各 fold 累计收益均值塞进 `oos_annual_return`，selector._score 把它当年化收益 `ann_return/target`（target≈0.20），把约 3 周累计收益(±2%)除以年化目标 → return_score 被系统性压向 0，return 维度区分力削弱，字段名误导。
- **实盘影响**：score 的 return 分量被隐性降权，策略排序偏向 Sharpe/回撤；但同一次评估内 fold 等长、缩放对所有候选一致，不翻转组内排名，资金权重(softmax 仅用 oos_sharpe)不受污染。hyperopt 接受闸门只用 oos_sharpe，不受影响。故为标定/可读性缺陷（MEDIUM~LOW 边界）。
- **修法**：聚合时按 fold 交易日年化，或 _score 里把 target 折算到窗口跨度；字段重命名 `oos_period_return`。

#### B3【LOW】Sharpe/Sortino/vol 恒年化但短窗 annual_return/calmar 不年化，同一报告口径不一致
- **位置**：`quanti/backtest/metrics.py:24-28,31,40,46,50`
- **问题**：n_days<60 时 annual_return 不外推（f19ca77 正确修复的副产物），但 sharpe/sortino/annual_vol 仍 ×√252、calmar 用未年化分子。同一短窗报告内"年化"量口径不统一。
- **实盘影响**：跨 fold/策略比较时各指标年化口径不一；但 sharpe 为无量纲比率且组合权重只用 sharpe（口径一致），影响局限于 composite 排序边际与展示。
- **修法**：短窗下对 sharpe/sortino/vol 同样不年化或加 warning，calmar 用与分子一致口径。

#### B4【LOW】买入两段式滑点缩量回退不重算冲击价（保守、无害）
- **位置**：`quanti/backtest/engine.py:381-391`
- **问题**：现金不足缩量循环里 price 固定为缩量前较大 qty 的冲击价，只重算 cost/commission。因冲击随 qty 单调增，对缩小后 qty 用偏高价 → 高估成本。
- **实盘影响**：纯保守近似，方向只让回测显得更差，不制造乐观偏差。LOW/informational。
- **修法**：缩量循环内对最终 quantity 重算一次 slippage。

#### B5【LOW】ADV 20 日窗口含当日成交额，开盘成交时点轻微前视
- **位置**：`quanti/backtest/engine.py:167`（amounts[max(0,i-19):i+1] 含当日）
- **问题**：当日成交额在开盘成交时点未知，属轻微前视。
- **修法**：ADV 窗口改为 `[i-20:i]` 不含当日。

### C. 回测≡实盘 (backtest-vs-live)

#### C1【HIGH】回测引擎完全不应用 -15% 组合回撤熔断，回测≢实盘
- **位置**：`quanti/backtest/engine.py:173-255`（日循环无 check_portfolio_stop/高水位追踪）；对照 `quanti/execution/paper_broker.py:483-502`、`quanti/agent/runtime.py:672-682`；`quanti/risk/manager.py:39,125-132`（阈值 -0.15）
- **问题**：实盘/纸面盘每 tick 在生成新信号前调 `enforce_portfolio_stop()`，净值自高水位回撤≤-15% 即 cancel_all_pending+flatten+halt agent。回测主循环只调 check_exits/check/protections.check_entry，从不调 check_portfolio_stop，无组合级高水位追踪与强制清仓（grep portfolio_stop/high_water 在 engine.py 零命中）。引擎里的 peaks 是逐票止盈用的最高价，非组合净值高水位。MaxDrawdown protection 仅锁新买入、不强平，不等价。
- **实盘影响**：回测可一路扛跌到 -20%/-30% 永不熔断；实盘则 -15% 处强平+永久停机。回测的最大回撤/Calmar/Sharpe/收益曲线系统性偏离实盘——既可能高估收益（吃到实盘停机后看不到的 V 形反弹），也错估尾部风险。用此回测决定上实盘/调参会对尾部风险做错误判断。
- **修法**：engine 日循环标记净值后、生成信号前维护 `peak=max(peak,total_value)`，调 `check_portfolio_stop`；触发则次日开盘（与回测成交口径一致）清空持仓并停止后续建仓，记熔断事件。
- **业界对照**：freqtrade protections 在 backtest 与 dry/live 共用同一引擎正是为消除此类背离。

#### C2【HIGH】回测建仓无视 signal.strength 与 sizer，与实盘按强度/波动目标定仓不一致
- **位置**：`quanti/backtest/engine.py:356-370`（max_spend=cash*0.95，受单票/总仓 cap，无 strength/sizer，min(affordable,10000) 硬顶）；`:325-326`（strength 在 _execute_buy 边界被丢）；实盘对照 `quanti/execution/paper_broker.py:530-539,713-720`（无 sizer 时 cash*0.95*clamp(strength,0.1,1)；有 sizer 按 target_weight）、`quanti/execution/qmt_broker.py:270`；强度来源 `quanti/agent/signal_pipeline.py:55-68`（strength=final_score）
- **问题**：回测几乎用满可用现金（仅受 cap）建仓，完全不读 strength，也无 PositionSizer。实盘按 strength 缩放。全部 6 个随附策略 BUY 信号 strength 恒为 0.7~0.85，agent 路径 strength=final_score 常落 0.35~0.5。
- **实盘影响**：同一 strength=0.5 信号，回测投 ~0.95×cash、实盘只投 ~0.665×cash；agent 路径回测投入约为实盘 2.5 倍。回测每笔规模/集中度/现金占用/收益与回撤幅度被系统性放大，直接误导上线与风控评估。`min(affordable,10000)` 硬顶在实盘三条路径均无对应，又在高价股压缩回测仓位。post-trade caps 在建仓早期/现金充裕时无法抵消该差异（strength 只额外砍实盘）。
- **修法**：回测建仓复用实盘同一套定仓逻辑（无 sizer 时 cash*0.95*clamp(strength)，或注入同一 PositionSizer 算 target_value）再受 cap 封顶；移除回测独有的 min(affordable,10000)。
- **业界对照**：freqtrade 铁律"回测=dry-run=live 同一条 sizing/下单代码路径"；vnpy/zipline 按 order_target 共用规模逻辑。

#### C3【HIGH】纸面/实盘成交路径无涨跌停/停牌可成交性闸门，与回测系统性背离
- **位置**：`quanti/execution/paper_broker.py:366-368,504-644,689-840`（取价直接 bar.open，全程无 prev_close/涨跌停/停牌判定）；`quanti/utils/market.py:107-124`（next_trading_bar 只判"有无更新 bar"）；对照 `quanti/backtest/engine.py:34-59,298-302`（_tradable_at_open 按板块限幅阻断一字板）；生产 `quanti/api/app.py:49` fill_mode="pending"
- **问题**：回测在一字涨停开盘阻断买入、一字跌停开盘阻断卖出并顺延/丢弃；PaperBroker 成交路径只要 bar 存在就按 open±slippage 成交，grep `_tradable_at_open/_limit_pct/prev_close/涨停/跌停` 在 execution/ 零命中。
- **实盘影响**：纸面/实盘假设在一字板顺利成交（真实 A 股无法成交）——追涨策略实盘买不到、止损单跌停板卖不掉却被记为已平仓。回测有闸门会跳过，paper/live"成交" → 轨迹系统性背离，净值与风险被高估。接 QMT 后是直接资金安全与决策误导。
- **修法**：把 `_tradable_at_open/_limit_pct` 抽到 utils 供两端共用，在 _fill_pending/_fill_buy/_fill_sell 取价前判定；BUY 涨停板/SELL 跌停板保持 pending（受 TTL 约束）。
- **业界对照**：zipline/backtrader 用 limit-price 或可成交量模型阻断不可成交撮合；vnpy/QMT 实盘涨跌停单被拒/挂队列不成交。

#### C4【MEDIUM】回测默认 VolumeImpactSlippage 与实盘 PaperBroker 固定 0.1% 滑点不一致
- **位置**：`quanti/backtest/engine.py:111-114`（默认 VolumeImpactSlippage）、`:351-376,444-447`；`quanti/backtest/slippage.py:61-98`；`quanti/execution/paper_broker.py:61,87`（默认 slippage=0.001 固定）、`:522,614,695,808`（固定比例成交）
- **问题**：回测默认按参与率惩罚（5bps+sqrt 冲击，封顶 300bps），实盘固定 10bps 与下单量无关。所有生产回测入口（cli/selector/api/mcp）与所有 PaperBroker 入口（cli/mcp/api）均未传 slippage，两端各走默认值，分叉真实生效。无共享定价函数。
- **实盘影响**：同一笔成交系统性不同价。典型小单（10000 股×~10 元对 ADV 数亿票，参与率~0.1%）回测约 6-7bps 略低于实盘 10bps，偏离仅个位数 bps；只有大单/薄量股才爬升到几十~上百 bps。但 selector/hyperopt 用回测净值筛选策略，成本口径分叉会污染选择结果。
- **修法**：统一注入同一 SlippageModel，或回测默认改回 FlatSlippage(bps=10)、VolumeImpact 仅作容量压测可选项；最稳妥抽共享成交定价函数被两端调用。

> **去重说明**：原 paper-03（"paper 固定 0.1% vs 回测冲击模型"）与 backtest-01 为同一根因，已合并为 C4。双视角对该问题给出 MEDIUM/LOW 分歧——因 VolumeImpactSlippage 在 1% 参与率处恰好标定为 10bps、且单票 10% 上限使常规组合参与率远低于 1%，数值偏差多为个位数 bps 且偏保守方向。取 MEDIUM（保守保留），因它会污染 selector/hyperopt 的策略筛选。

#### C5【HIGH】实盘 QMT 持仓 market_value 用成本价 → pnl 恒为 0 → 逐票止损/移动止盈实盘永不触发
- **位置**：`bridge/vnpy_backend.py:182,188`（market_value=round(vol*avg,2) 成本市值，从不回报现价）；`quanti/execution/qmt_broker.py:101-103`（cur=mv/vol≡avg）、`:323-328`（check_exits 硬编码 peaks={}、strategy_sell=set()）；`quanti/models.py:113-116`（pnl_pct）；`quanti/risk/manager.py:171,183`（止损/移动止盈门控）；mock 同 bug `bridge/qmt_bridge.py:134`；掩盖测试 `tests/test_qmt_broker.py:216-218`
- **问题**：vnpy_backend.positions() 把 market_value 写成成本市值，QmtBroker 以 mv/vol 反推现价 → current_price 恒等于 avg_cost → pnl_pct≡0。止损判据 `pnl_pct<=stop_loss_pct`（负阈值）永不成立；移动止盈 `pnl_pct>=activate` 且 peaks 恒空 → 双重失效；策略离场被空集禁用。snapshot_portfolio 的 total_value 走 asset()（live 正确），故组合熔断没坏，坏的是逐票止损这条核心安全网。对照 `paper_broker.py:854-858` 正确刷新 current_price + 传真实 peaks。
- **实盘影响**：实盘单只股票暴跌也不会被 check_exits 卖出，最大单票亏损不受 stop_loss_pct 约束。paper/回测止损正常 → 上线后行为骤变，造成超预期单票亏损。这是实盘风控最重要安全网的静默失效。
- **修法**：vnpy_backend.positions() 用 xtdata get_full_tick lastPrice（`vnpy_backend.py:290-291` 已有现价源）填 market_value=vol*last 并单列 last_price；_reconciled_portfolio 优先消费现价字段；mock 用 quote 价驱动现价；删掉只验 0 的虚假断言，补"价跌破止损线→check_exits 返回 SELL"测试。

#### C6【MEDIUM】实盘 SELL 限价基于成本价而非现价（qmt-01 衍生）
- **位置**：`quanti/execution/qmt_broker.py:227`（price=pos.current_price*(1-slippage)）、`:323-333,383-403`（check_exits/flatten/portfolio_stop 全经此路径）
- **问题**：受 C5 影响 current_price≡avg_cost，故止损/清仓/急停的限价单价格=成本价×(1-滑点)，与市价脱钩。BUY 路径用 _latest_price（现价）而 SELL 用成本价，买卖不对称证明是疏漏。
- **实盘影响**：止损场景现价远低于成本时限价高于市价（仍可成交但价不可控）；现价高于成本时限价过低可能挂不出。削弱熔断/止损成交确定性。
- **修法**：SELL 价格改读 _latest_price/quote.last；急停/止损宜用市价或现价向下加价限价。随 C5 一并修。

#### C7【LOW】部分成交后被撤单时本地镜像丢失已成交数量，partial 状态永不落镜像
- **位置**：`quanti/execution/qmt_broker.py:309-320`；`bridge/vnpy_backend.py:88,236`（PARTTRADED→'partial'，filled_volume=o.traded 可用却未消费）；`quanti/data/database.py:1020-1037`（update_order_status 不写 filled 字段）
- **问题**：只在 venue status=='filled' 时回写成交量；'partial' 落入 still_pending，PARTTRADED→CANCELLED 时标 cancelled 且 filled_quantity 仍 0，丢失真实部分成交。资金/持仓以券商对账为准（_reconciled_portfolio 读 /trader/positions），故仅审计/UI 失真，非资金致命。
- **修法**：对账时 filled/partial/cancelled 一律回写 venue traded 数量，partial 保持 pending 并更新已成交量，cancelled 保留已成交部分；补 partial-then-cancel 测试。

### D. 前视偏差 (look-ahead)

> 本桶无独立 HIGH 新发现。回测"次日开盘成交"消除同 bar 收盘前视（dcdc388）已正确落地。残留仅 B5（ADV 含当日，LOW）。因子 point-in-time 行业分类用当前行业（`cross_sectional.py:170`）做历史 IC 评估属轻微失真（P3，见对标 O 区）。DSL `Ref n<0` 被禁、挖掘 train/OOS gap 扣 `fwd_days*2+3`（`factor_miner.py:90`）均正确。

### E. 过拟合 (overfitting)

#### E1【MEDIUM】默认 OOS fold 仅约 15 交易日，Sharpe 估计统计无意义却驱动评分与实盘资金权重
- **位置**：`quanti/agent/selector.py:112(test_days=21),216-232(pick_topk softmax over oos_sharpe),229(temp=1.0),303`；`quanti/agent/walk_forward.py:88-90(日历日切窗),147(仅<5 bar 守卫),165,187`；`quanti/backtest/metrics.py:39-40`（Sharpe 无最小样本守卫）
- **问题**：默认 OOS≈15 交易日，用其日收益估 Sharpe 样本极小、标准误巨大。3 fold Sharpe 取均值成 oos_sharpe，既进 _score 又是 pick_topk softmax 权重唯一输入，直接决定实盘 top-K 资金分配（`runtime.py:471,481` 确认进入信号融合）。softmax 温度 0.5→1.0 只能展平不能消噪；selector 无 hyperopt 的 min_folds 守卫。
- **实盘影响**：实盘资金分配建立在 ~15 天 Sharpe 噪声上，重跑/换 universe 时优胜策略与权重可能剧烈翻转。max(0,·) 地板化+total≤0 退化等权+多项 _score 是部分兜底，故 MEDIUM 而非 HIGH。
- **修法**：test_days 调到产生 ≥40-60 交易日窗口（或减少 fold 换更长窗口）；selector 引入 min_folds 守卫；用"合并所有 fold OOS 日收益再算一个 Sharpe"提升样本利用率。
- **业界对照**：zipline/pyfolio、Qlib rolling 的 OOS 段普遍 ≥1 季度；样本不足时不输出 Sharpe 排序。

#### E2【MEDIUM，对标 O1】walk-forward fold 用自然日切窗、折间无 embargo gap → OOS 独立性削弱
- **位置**：`quanti/agent/walk_forward.py:87-92`（timedelta 自然日，test_days=21，warmup=120，折间无 gap）
- **问题**：21 自然日≈15 交易日 OOS 过短；相邻 fold 无 gap，warmup 段与上一 fold test 段重叠 → 持仓跨窗造成 OOS 间信息泄漏。hyperopt train 窗已做 gap，但 fold 之间本身没 gap。
- **实盘影响**：选出的策略 OOS alpha 偏乐观。
- **修法**：fold 改用 trade_calendar 交易日切分；相邻 fold 留 embargo（≥最大持仓天数）；提高 test_days 或减少 fold。
- **业界对照**：purged K-fold（López de Prado）在 train/test 间留 embargo/purge gap。

#### E3【MEDIUM，对标 O2】Selector IS 窗口与 OOS folds 高度重叠 → 排名偏乐观
- **位置**：`quanti/agent/selector.py:138`（IS=end-365 到 end，与 OOS folds 时间窗重叠）、`217`
- **问题**：用同一段最近数据既训练又评估；topk 权重来自仅~15 交易日折的 OOS Sharpe 点估计。
- **修法**：IS 窗口应早于所有 OOS folds（留 gap）；topk 权重改用 fold 间 Sharpe 下分位或等权+仅保留显著为正者，不用单点 Sharpe softmax。

#### E4【MEDIUM，对标 O3】因子挖掘 OOS-IC 固定阈值 0.03，无多重比较校正
- **位置**：`quanti/agent/factor_miner.py:127`（oos_ic≥0.03 即接受，n_candidates=10）、`:131`（redundancy 只防与已接受相关）
- **问题**：LLM 一次提 10 个各独立过 0.03 gate，无 Bonferroni/FDR 校正，提的越多纯靠运气过阈值越多。OOS≈43 交易日，rank-IC 均值标准误不小，0.03 显著性未验证。redundancy 不防与市场 beta 相关或对未来不稳定。
- **修法**：门槛改 ICIR 或 IC 序列 t 检验；按候选数做 FDR 校正；redundancy 加"对现有 composite 正交后增量 IC>0"。
- **业界对照**：Qlib RD-Agent 对挖出因子做 IC 的 t 统计、ICIR、正交后增量 IC、多重检验校正。

#### E5【LOW】consistency 用 n=3 总体标准差(ddof=0)、零均值回退用绝对 std，跨 test_days 不可比
- **位置**：`quanti/agent/walk_forward.py:173-181`；`quanti/agent/selector.py:292,304`（w_consistency=0.4）
- **问题**：std=np.std(ddof=0) 在 n=3 低估离散度；|mean|<0.01 回退 max(-1,-std) 用未年化累计收益绝对 std，量纲随 test_days 变。
- **实盘影响**：同一次运行内 fold 等长、仅同运行内排序，"跨 test_days 不可比"实际不可达；权重 0.4 且 CoV 方向正确。LOW。
- **修法**：小样本(<3 有效 fold)关闭/降权 consistency；回退分支用同量纲标准化离散度；ddof=1。

#### E6【MEDIUM，对标 O4】hyperopt 用 train Sharpe 选 combo，min_trades_oos=5 门槛偏低，网格超 64 纯随机采样
- **位置**：`quanti/agent/hyperopt.py:37(build_grid >64 随机采样),71(min_trades_oos=5),101-113`
- **问题**：train Sharpe 选 combo 有过拟合倾向（OOS 必须超 default 这道闸是对的缓解）；5 笔 OOS 交易的 Sharpe 不可信；随机采样可能漏掉网格好区域。
- **修法**：min_trades_oos 提到 ~20；combo 超限优先保留网格边界点；用 OOS Sharpe 稳健下界做接受判定。

### F. 逻辑错误 (logic-error)

#### F1【HIGH】加仓保留旧 buy_date，当日新买入股份可同日卖出，破坏 T+1
- **位置**：`quanti/execution/paper_broker.py:759-764`（_fill_buy 加仓 buy_date=existing or bar_date）、`:564-571`（_fill_pending 加仓）；T+1 判定 `:802-805,607-611`；整笔卖出 `:807,613`；`quanti/data/database.py:873-898`（upsert ON CONFLICT 写入旧 buy_date）；对照正确实现 `quanti/execution/qmt_broker.py:210-223`（SELL 按 can_use_volume 封顶）
- **问题**：加仓刻意保留原始 buy_date；T+1 只比 `pos[buy_date]==bar_date`；卖出 quantity=整笔。一周前建仓的仓位今日加仓后 buy_date 仍是旧日期，T+1 检查通过 → 今日新增量在同一交易日被卖出。生产 pending 模式下需加仓 BUY 与 SELL 同 bar 成交才触发。
- **实盘影响**：破坏 A 股 T+1 硬约束；回测按 bar 序天然不会同 bar 卖当日买入量 → paper-vs-live 背离。注：真实 QmtBroker 是独立 venue，SELL 一律 can_use_volume 封顶（`qmt_broker.py:216`），不会向券商提交超卖单，故"QMT 对账失败"论断不成立——危害局限于模拟盘记账偏乐观（双视角对此从 HIGH 下调到 MEDIUM 有分歧，取 HIGH 因这是 backtest-vs-live 不变量破坏且应在接线前对齐）。
- **修法**：仓位记录 last_buy_date 或当日冻结量，卖出 quantity 受 T+1 可卖上限约束，与 QmtBroker can_use_volume 口径一致。
- **业界对照**：rqalpha/聚宽按建仓批次维护 T+1 冻结，卖出只允许 sellable=持仓−当日买入。

#### F2【MEDIUM】max_daily_trades 把卖出计入当日预算却只拦截买入，止损密集时反噬买入额度
- **位置**：`quanti/risk/manager.py:77-78`（SELL 直接放行）、`:96-97`（仅 BUY 受限）、`:200-203`（record_trade 无 direction 无条件自增）；调用方对买卖都计数 `paper_broker.py:585,635,783,832`、`backtest/engine.py:333`、`qmt_broker.py:240`
- **问题**：当日卖出（止损/移动止盈/策略离场/kill-switch flatten）全部消耗 max_daily_trades 预算，但只有买入受其拦截。runtime 顺序"先 check_exits 后 execute_signals"会让卖出先吃额度。行情大跌批量止损当天，系统恰因卖得多而拒绝调仓/再平衡买入——风控在最需灵活时自我锁死。
- **实盘影响**：语义违反文档（"日内交易上限"通常指开仓次数）与直觉；默认 20 较宽松，需高换手日才 binding，故 MEDIUM。
- **修法**：record_trade(direction) 仅 BUY 计数（推荐，=日内开仓上限），或 check() 对 SELL 也校验为 total-trades-per-day cap。
- **业界对照**：freqtrade 的开仓/下单频率限制区分开仓与平仓，平仓不占开仓配额。

#### F3【LOW】挂单成交丢失 strength（pending 模式每笔按满仓信念 1.0 定仓）
- **位置**：`quanti/execution/paper_broker.py:343-349`（重建 Signal strength=1.0）；定仓用此 strength `:533,538`；orders 表无 strength 列（`database.py:296-311`，insert_order 不写），`_record_order:666-687` 仅存 queued_strength 入 decision-log；信号源 `signal_pipeline.py:67`（strength=final_score）；生产 `api/app.py:49` fill_mode=pending
- **问题**：pending 重建 Signal 时硬编码 strength=1.0，与 immediate 路径（用真实 strength）分叉。注意：去重忽略 strength 这一半（原 paper-05 headline）经验证被证伪——pending 成交恒填 1.0，与去掉的强弱信号无关，两半互相抵消。真正缺陷是 pending 路径丢弃 conviction。
- **实盘影响**：低 conviction 买入被放大到满仓单票上限；受单票 10%/行业/总仓硬上限兜底，仅影响定量精度。与 C2 同属"实盘建仓规模与设计/回测意图不符"，但这是 pending-vs-immediate 内部分叉。LOW。
- **修法**：orders 表新增 strength 列并持久化，try_fill_pending_orders 用 order_row strength；补 pending 成交数量随 strength 缩放断言。

> **去重说明**：原 runtime-01（HIGH）与 paper-05（LOW）描述同一 pending 丢 strength 问题。runtime-01 视角主张 HIGH（低信念候选 final_score=0.3 本应投 28.5% 实投 95%），paper-05 视角证伪了"去重丢强信号"那一半。合并为 F3，严重度取保守评估：因受多重硬上限兜底且属定量精度，列 LOW，但其与 C2 共同构成"实盘建仓规模与回测/设计不符"的系统性问题，修复优先级随 C2。

### G. 实盘安全 (live-safety)

#### G1【MEDIUM】is_connected 在 mock 模式也报已连接、不校验 trader_connected；mock 成交被镜像成真实成交
- **位置**：`quanti/execution/qmt_broker.py:76-85,182-256`；`bridge/qmt_bridge.py:108-117`（health 恒 ok=True）、`:147-149,197-238`（mock fallback/_mock_fill）；契约 `quanti/execution/base.py:88-95`
- **问题**：is_connected() 只判 health.ok（mock 恒 True），忽略 mode/trader_connected（docstring 自承 stricter gate 未实现）。xtquant/vnpy 导入失败被静默吞掉 → bridge 无声落回 mock，下单在 _mock_fill"成交"，QmtBroker 当真实成交镜像（改本地 cash/持仓、status=filled、record_trade）。违反 base.py 契约（不可达时应返回 False）。
- **实盘影响**：接线后 QMT 环境异常致静默回退 mock 时，系统以为在实盘下单并报成交，实则空单未达券商，账面与券商真实状态脱节，决策建立在幻觉成交上。当前无 runtime 实例化 QmtBroker，故为"接线即触发"陷阱，MEDIUM。
- **修法**：实盘模式下 is_connected() 要求 mode=='vnpy' 且 trader_connected is True，mock 视为未连接；_submit_signal 加 mode 守卫，mock 拒绝镜像为 filled；补 mode==mock 时 is_connected()==False 测试。

#### G2【MEDIUM/LOW】实盘日内交易上限只在进程内计数，重启即清零、从不从 /trader/trades 回种
- **位置**：`quanti/execution/qmt_broker.py:67(每次新建 RiskManager),236-240(只 record_trade，回种标注 phase-③)`；`quanti/risk/manager.py:60,96-97,200-203`；文档宣称生效 `docs/USAGE.md:1087`（每日最多 20 笔）
- **问题**：计数纯内存，每次新建 QmtBroker（agent 运行/进程重启/定时任务）从 0 起，券商当日已成交笔数不计入。cb55ea7 的按日历日归零是另一方向问题，不解决此处。属被宣称生效、实则可绕过的安全限制。
- **实盘影响**：当日反复重启可绕过 max_daily_trades 硬上限。默认 20 较宽松且当前无真金可亏，故 MEDIUM~LOW。
- **修法**：session 启动时从 /trader/trades 按交易日历日回种计数，或日内计数持久化到 DB 按日读取。
- **业界对照**：vnpy gateway 启动时从券商拉全量持仓/当日成交做对账，本地计数从真实成交重建。

#### G3【LOW，对标 L2】组合熔断 high-water mark 依赖快照表，paper↔live 复用同一 db_path 会污染峰值
- **位置**：`quanti/execution/paper_broker.py:489` / `qmt_broker.py:410`；`quanti/data/database.py:141,326,1077`
- **问题**：peak 取 portfolio_snapshots 历史 MAX；若 paper 与 live 复用同一 db_path，paper 期高净值成为 live high-water，live 一上来即"已回撤"误触熔断。
- **修法**：部署强约束 paper/live 用不同 db_path + 启动校验；或 snapshot 表加 account_mode 列隔离峰值。

#### G4【MEDIUM，对标 L3】QmtBroker 限价 round(...,3) + 未 clamp 涨跌停/tick-size，主板会报非法价位被废单
- **位置**：`quanti/execution/qmt_broker.py:231,227,273`
- **问题**：A 股主板最小报价 0.01 元，round(...,3) 对主板报出 12.345 被废单；限价未 clamp 到当日涨跌停内。
- **修法**：按板块 tick-size 取整（主板 0.01），clamp 到 [跌停,涨停]。
- **业界对照**：rqalpha/vnpy 对 tick size 取整、价格 clamp 到涨跌停。

#### G5【LOW，对标 L4】kill-switch flatten 无幂等保护，撤单未确认即重复下卖单的竞态
- **位置**：`quanti/execution/qmt_broker.py:389,413-414`
- **问题**：cancel_all_pending→flatten 顺序对，但 flatten 从 reconcile 拿持仓逐个 SELL，若撤单未在券商生效或刚成交未刷新，可能漏卖/重复卖，无 client order id 去重。
- **修法**：撤单后轮询确认全部 cancelled、再 reconcile 取最新 sellable 才下平仓单；每单带幂等 reference。

#### G6【LOW，对标 L1】QmtBroker"提交即成交"假设 + 未接 xtquant 异步成交回调
- **位置**：`quanti/execution/qmt_broker.py:234`（filled=accepted and status=="filled"），try_fill_pending_orders 轮询 /trader/orders（注释标 phase-③ 未完成）
- **问题**：假设提交即成交，真实异步回报靠轮询，代码注释自承未完成。
- **修法**：实盘前接 xtquant on_order/on_trade/on_position 异步回调状态机，别用"提交即成交"。

#### G7【LOW】blocked_prefixes 声称屏蔽 ST 股实为死配置（误导性安全限制）
- **位置**：`quanti/risk/manager.py:41`（带注释 "Block ST stocks"，全库无任何读取处）；真正可用的 ST 过滤在 `quanti/agent/universe.py:61,170-173`（按 name），但默认关闭（`runtime.py:295` liquidity_filter 默认 False）且自选 pool 跳过（`runtime.py:285-288`）
- **问题**：RiskConfig.blocked_prefixes 从不被任何拦截逻辑读取；即便读取，前缀匹配的是代码而 ST 是名称前缀，逻辑上也匹配不到。运营者会误以为风控层屏蔽 ST。另有按 name 正确实现的过滤但默认关闭/自选 pool 旁路。
- **修法**：在 risk.check/_entry_allowed 按 stock.name 判 ST/*ST 拒绝 BUY 并对 ST 用 ±5% 限幅（防御纵深），或删除该死配置与注释消除误导。
- **业界对照**：聚宽/rqalpha 通过 is_st 字段过滤。

---

## 三、存疑待查 (uncertain)

- **win_rate 语义**（`quanti/backtest/metrics.py:60`）：win_rate=(日净值收益>0).mean() 是"上涨日占比"非交易胜率，机制属实但 live_impact 中"被 selector/调参/挖掘当闸门助长牛市过拟合"经 grep 证伪——win_rate 唯一消费方是前端展示标签（`web/src/views/Backtest.vue:269`），不进任何决策闸门。残留为 UI/命名误导（LOW）。建议改名 daily_up_ratio 或补真实 trade_win_rate。
- **F3 strength 还原可行性**：建议"从 order_row 还原 strength"不可直接实现——orders 表不持久化 strength 列，需先建列或解析 decision-log。待定方案。

---

## 四、已证伪 / 已修复（证明审查到位）

历史修复经核对在当前代码真实生效：
- 回测"次日开盘成交"消除同 bar 收盘前视（dcdc388）✓
- 风控行业 30%/单票 10%/总仓 80% 事后口径真正封顶（38e46c7）✓
- -15% 回撤熔断：高水位触发清仓+暂停 agent（a00ec75）✓ **但仅在 paper/live 生效，回测引擎缺失 → 见 C1**
- 日内交易上限按日历日归零（cb55ea7）✓ **但只解决"满额永久锁死"，未解决 F2 买卖不对称、G2 重启绕过**
- metrics 短窗不外推年化消除 933% 噪声（f19ca77）✓ **但引入 B2/B3 口径不一致副产物**
- selector 权重温度 0.5→1.0 + 最小 OOS fold 守卫（b7f14d6）✓ **只展平不消噪，E1 根本问题仍在**
- db.execute() 锁内物化消除跨线程脏读（7be797d）✓

证伪：
- "去重忽略 strength 导致按弱信号定量"（原 paper-05 headline）：pending 成交恒填 strength=1.0，与去重丢弃的强弱信号无关，两半抵消，headline 不成立（真实缺陷见 F3）。
- "win_rate 作为过拟合闸门"：实际仅前端展示，不进任何决策路径。
- DSL 任意代码执行/未来引用风险：parser 白名单 + `Ref n<0` 被禁正确防护，无需改。
- 因子挖掘标签泄漏：train/OOS gap 扣 `fwd_days*2+3` 正确（`factor_miner.py:90`）。

---

## 五、业界对标改进点（按优先级）

| 优先级 | 编号 | 改进点 | 对应确认问题 |
|---|---|---|---|
| P0 | D1+D2 | qfq 增量拼接断层 + 双源复权口径未对齐（改 hfq 或原始价+复权因子表） | A1/A2 |
| P0 | B1 | 抽统一 position_sizer 供回测/两 broker 共用，删回测 10000 股硬顶与忽略 strength | C2/F3 |
| P0 | L1 | QMT 接 xtquant 异步成交回调 + 启动从 /trader/trades 对账重建日内计数 | G6/G2 |
| P1 | O1 | walk-forward fold 改交易日切窗 + 折间留 embargo gap | E2 |
| P1 | O2 | Selector IS 窗早于所有 OOS folds；topk 权重用稳健统计非单点 Sharpe softmax | E3/E1 |
| P1 | O3 | 因子挖掘门槛改 ICIR/t 检验 + FDR 多重比较校正 + 正交后增量 IC | E4 |
| P1 | B2 | 回测加单 bar 成交量上限（≤当日量 N%）；ST 识别 ±5%；文档化限价模型差异 | B1/C3 |
| P1 | L2 | paper/live 状态库隔离（不同 db_path 或 account_mode 列） | G3 |
| P2 | O4 | hyperopt min_trades_oos 提到 ~20；combo 超限保留网格边界点 | E6 |
| P2 | B3/D3/D4 | 回测 T+1 分笔 sellable 对齐 can_use_volume；volume 单位统一为股；交易日历作主时钟 | F1/A3/A4 |
| P2 | L3/L4 | QMT 价格 tick-size 取整+涨跌停 clamp；flatten 加幂等 reference | G4/G5 |
| P2 | 架构 | 引入 rqalpha mod / Qlib executor-exchange 分层，撮合规则做可插拔共享对象（消除 backtest≠live 的结构性解法） | C2/C3/C4/F1 |
| P3 | 性能/PIT | engine 预建 {(code,date):bar} 字典；行业分类 point-in-time（历史 IC 用当时行业） | — |

---

## 六、上实盘前必须修复的 HIGH 清单

- [ ] **C1 回测引擎应用 -15% 组合回撤熔断**（`engine.py:173-255`）：维护组合净值高水位，触发则次日开盘清仓+停建仓，与 paper/live 同口径。否则回测的回撤/Calmar/Sharpe 不能代表实盘尾部风险。
- [ ] **C5 修复实盘 QMT 持仓现价**（`vnpy_backend.py:188`、`qmt_broker.py:101-103`）：用 xtdata lastPrice 填 market_value，让 pnl_pct 反映现价。否则实盘逐票止损/移动止盈永久失效，单票暴跌不卖。**连带修 C6**（SELL 限价改读现价）。
- [ ] **C2 统一回测/实盘建仓 sizing**（`engine.py:356-370`）：回测按 signal.strength（或同一 PositionSizer）定仓，移除 min(affordable,10000) 硬顶。**连带修 F3**（pending 持久化 strength）。否则回测投入约为实盘 2.5 倍，selector 选的策略 ≠ 实盘表现。
- [ ] **A1+A2 统一复权口径**（`vnpy_backend.py:259-274` 传 dividend_type='front'，或改 hfq/原始价+复权因子表）：否则 xtdata 增量同步在除权点污染历史价，回测数字本身不可信。
- [ ] **C3 纸面/实盘成交加涨跌停/停牌可成交性闸门**（`paper_broker.py:366-368,504-840`）：复用回测 `_tradable_at_open`。否则实盘在一字板"成交"而真实买不到/卖不掉，止损被记为已平仓、风险被高估。
- [ ] **F1 加仓 T+1 不变量**（`paper_broker.py:759-764,564-571`）：记录 last_buy_date/当日冻结量，卖出受 T+1 可卖上限约束，与 QmtBroker can_use_volume 对齐。

> 接线 QMT 前的 live-safety 阻断项（虽当前未实例化，接线即生效）：**G1**（mock 不得报已连接/不得镜像成真实成交）、**G2/G6**（日内计数对账 + 异步成交回调）。建议与上述 HIGH 同批处理。

---
*报告完。所有条目均附 file:line，可逐条回溯当前代码核对。*
