# 任务：回购公告事件 alpha 研究（repurchase event drift）

## 背景（先读）

- 存活策略：`strategies/sse_index_enhance.py`（PR #197，结构性恒等式超额）。
- 已证否清单（docs/2026-07-01-can-this-system-make-money.md + docs/2026-09-11-*.md +
  docs/2026-09-12-fcfy-alpha.md）：截面因子 composite（13 因子融合）、防御倾斜、
  ML 截面、择时、宽度、可转债、市场中性 OOS、红利TR增强、打新 overlay@百万级、
  FcfY(FCF/市值)。**回购事件不在其中** —— 事件驱动族只测过打新（供给侧）与
  盈余修正（弱信号未达闸），回购（公司行为信号）从未测过。
- 这是「新数据接口带来的未测信号」：tushare `repurchase` 接口（2026-09-13 验证
  可用：按 ann_date 范围查询返回全市场回购事件流水，含 预案/股东大会通过/实施/
  完成/停止 五态，ann_date 天然 PIT）。数据层 `scripts/backfill_repurchases.py`
  已提交（commit 36cc92e），**回填进程正在另一个终端写
  /opt/data/quanti/data/market.db 的 repurchase_events 表**（全历史 2018-01 起，
  预计 ~5 分钟完成；进度 /tmp/rp_progress.json，完成标记 /tmp/rp_done.json，
  done 时 status=done 且 fails=[]）。开发期先用已回填子集写代码 + fake 测试；
  正式全量跑**必须轮询等 /tmp/rp_done.json 出现**（每 60s 一次，最长 30 分钟；
  超时则用已有数据跑并在文档标注「正式数字待补跑」+ 补跑命令）。

## 动机：外部证据表（写进研究文档，与本地实测并排对照）

| 来源 | 口径 | 结果 |
|---|---|---|
| Ikenberry, Lakonishok & Vermaelen 1995 (JFE, 被引 3100+) | 美股开放市场回购公告后 4 年买入持有 | 平均异常收益 +12.1%；市场反应不完全=长期漂移 |
| 湖大报社科版 2021（A股 2011-2019，316 事件，市场模型） | 回购公告 CAR[-1,1] | 均值 +2%，t=5.46，1% 显著 |
| 中金 2025「十问十答：A股事件驱动」 | 事件研究法 CAR 框架 | A 股多数事件 T+1 内充分定价；超预期类才有 T1-T10 漂移（+1.8%、胜率 55.7%） |
| 证券时报 2026-08-05（万得口径复盘） | Wind 股票回购指数（近 6 个月发市值管理回购的成份股）2018-07 至今 | 累计 +110% vs 上证 <40%；2021-2026 每年跑赢上证 |
| 源达证券 2025-03（增持类比） | 首次增持公告后 90 日 vs 万得全A | +3.4%；<50 亿市值组 +7.0%；低 PB 分位组 +4.6% |
| 证券时报 2026-08（同一篇） | 停止回购事件 | 金风科技终止后 5 日 -7%，终止公司发布以来跌幅均 >10% ⇒ 预案有「画饼」尾部风险 |

为什么不能直接搬：以上是指数/自家样本口径；预案≠落地（A 股「重预案轻落地」）；
漂移文献是**长期**（1-4 年）收益，T+1 散户可交易的中短期窗口（20-60 日）证据
只有事件研究 CAR。用自己的数据、自己的成本模型、DSR/PBO 闸重做才算数。

## 数据层（正在回填，勿改）

`scripts/backfill_repurchases.py` 逐月拉 tushare `repurchase`（2018-01 起，
单次 2000 行截断自动按旬/日细分），落 market.db 表：

    repurchase_events(code, ann_date, end_date, proc, exp_date,
                      vol, amount, high_limit, low_limit)
    -- proc ∈ {预案, 股东大会通过, 实施, 完成, 停止}
    -- code=6位代码; ann_date=公告日(YYYY-MM-DD, PIT 关键)
    -- amount=元：预案行为拟回购金额下限、完成行为已回购金额；
    --    **amount=-1.0 是缺失哨兵**（主键不允许 NULL），研究层当 NaN 处理
    -- exp_date=预案到期日（仅部分预案行有值）

## 要做什么

### 第一步：事件研究 `scripts/repurchase_event_study.py`

评估窗 = 本地行情覆盖（daily_quotes 2021-09-13 起 ⇒ 事件 ann_date ∈
[2021-10-01, 2026-09-10]；行情末 2026-09-10，长窗按可得 bar 截断并如实报 n）。

1. **事件定义（主口径）**：`proc='预案'` 行。同一 (code, ann_date) 若多行按
   (code, ann_date) 去重取一条（amount 取最大）；同票多次预案算独立事件
   （组合层再处理重叠）。辅助口径（全部入账为 trial）：`proc='股东大会通过'`、
   `proc='完成'`（该票首个完成行）。
2. **T+1 可交易口径（前视红线）**：事件参考日 D0 = **晚于** ann_date 的第一个
   交易日（announcement 盘后发布是常态，ann_date 当天收盘不可得）。CAR 从 D0
   **开盘**起算：D0 首日收益 = close(D0)/open(D0) − 1（hfq 复权口径：open 与
   close 同乘 adj_factor，比值不变），之后 close-to-close；基准 = 同期全市场
   等权日收益（daily_quotes 全票日收益均值，与 fcfy 研究同源）。
   CAR(1,t2) = Σ(D0..D0+t2−1 的超额)。窗口 (+1,+5)、(+1,+20)、(+1,+60)。
3. 统计：均值、NW t（滞后 = 窗长−1）、胜率（末值>0 占比）、逐年（2022-2026）
   均值与事件数、2021-2023 vs 2024-2026 两段对比（拥挤度检查）。
4. **清洗**（与 fcfy_study 同源）：剔金融（industry 含 银行/保险/证券，stocks
   表 industry 字段——查 fcfy_study.py 怎么取就怎么取）；剔公告日时点上市
   <120 交易日次新（daily_quotes 该票首行日期）；剔 ann_date 后无任何行情；
   ST 用 name_history PIT 判定（ann_date 时点含 ST 曾用名则剔）；退市票保留。
5. **预声明切片（都是 trial，全进 DSR 账本）**：
   - 规模三档：公告日 total_mv tercile（daily_basic，取 ann_date ≤ D 最近一行，
     万元口径）；
   - 力度两档：amount/total_mv(换算成同单位后) ≥ 0.1% vs < 0.1%（仅预案行）；
   - 三种 proc 口径。
6. **门槛（先过才有第二步）**：主口径 CAR(+1,+20) 均值 >0 且 NW t ≥ 2.5 且
   胜率 ≥ 55% 且 2022-2026 逐年均值 ≥4/5 年为正。主口径不过但某个**预声明**
   切片过 ⇒ 允许进第二步（该切片升为主口径，账本如实记全部多重性）；全灭 ⇒
   证否文档收尾（照 docs/2026-09-12-fcfy-alpha.md 格式）。

### 第二步：组合级回测 `scripts/repurchase_bt.py`（第一步过闸才写）

- 信号：每 20 交易日调仓日 D，候选 = ann_date ∈ (D−20, D] 的**新预案**（剔金融
  /ST/次新同第一步；剔该票近 12 个月内有 proc='停止' 记录的）；按力度
  amount/total_mv 降序 top-30，等权；次日 open 成交。持有：每期重选，掉出
  候选名单即卖（次日 open）；每票最长持有 60 交易日强制卖。
- 成本：向量化近似与 fcfy_backtest 同源（佣金万 2.5+5 元下限、印花税分段
  2023-08-28 前千1 后万5、过户费十万分之一、冲击 5bp+5bp√参与率按信号日前
  20 日均额、红利税 dv_ratio×持有年数×三档近似）。100 万资金。
- 基准：全市场等权（主）；有本地指数日线就用沪深300 作参考基准（没有就明说）。
- 报告：年化超额、TE、IR、最大回撤、年单边换手、逐年超额、每期候选数分布
  （2021-2023 事件稀疏 ⇒ 组合层该段数字如实标注意义有限）。

### 第三步：过拟合闸（`quanti/backtest/overfit.py`，别造轮子）

DSR trial_sharpes = 全部实测变体（3 proc × 3 窗口 × 预声明切片 + 组合层网格），
PBO(cscv, 16 splits) 同一组 trial。**账本诚实是闸有效的前提。**
**验收闸：DSR ≥ 0.95 且 PBO ≤ 0.2 且 年单边换手 ≤ 300% 且 逐年超额 ≥0 年数
≥ 4/5（2022-2026）**。全过 → 写 `strategies/repurchase_alpha.py`（signal=目标
权重，selectable=False，仿 sse_index_enhance 的接口）+ 回测 JSON 落 data/
（不提交）。任一不过 → 停在研究层，证否文档收尾。**证否文档就是有效交付。**

### 第四步：测试 `tests/test_repurchase_study.py`（+ bt 存在则 `tests/test_repurchase_bt.py`）

全注入 fake（临时 sqlite：手搓 trade_calendar/daily_quotes/daily_basic/
repurchase_events/stocks/name_history 行），零触网零真库（tushare import 不许
出现在被测试路径运行时）。覆盖：D0=晚于 ann_date 首个交易日（周末/节假日公告
用例）、D0 开盘起算 CAR 手算对账、(code,ann_date) 去重 amount 取最大、多次预案
独立事件、amount=-1 哨兵当缺失、PIT（ann_date 晚于 D 的行不可见）、万元/元
换算、金融/ST/次新剔除、bt 的新预案入池+停止剔除+60 日强退+成本公式。

### 第五步：研究文档 `docs/2026-09-13-repurchase-alpha.md`

诚实回答：(a) 预案/股东大会/完成哪个时点有信息、市场是否在 D0 内已充分定价
（对比中金「多数事件 T+1 内定价完成」）；(b) 漂移是否衰减（2021-23 vs
2024-26 两段——回购热 2024 年后，拥挤度）；(c) 停止回购负 CAR 是否值得做成
排除项；(d) 若证否：死因是漂移不存在、成本吃光还是闸不过。外部证据表与本地
数字并排对照。局限与已知偏差一节如实写（行情起点 2021-09、事件右截断、
停牌可成交性未建模、与 fcfy 文档同等诚实度）。

## 约束（禁止事项）

- **前视是死刑**：事件池只用 ann_date ≤ D 的行；CAR 不许含 ann_date 当天及
  之前的收益；切片阈值用公告日当时 total_mv。
- 闸值不得自行放宽；DSR 账本不得只报过闸变体。
- 不修改 `scripts/backfill_repurchases.py`；不动 market.db 既有表与
  repurchase_events 数据（研究层只读，回填归回填进程管）。
- 不碰 `strategies/`、`quanti/` 既有文件（过闸才新增 repurchase_alpha.py）。
- 测试不许触网。python 用 `/opt/data/quanti/.venv/bin/python`；market.db 只读
  引用 `/opt/data/quanti/data/market.db`；不要跑 `quanti sync`；不要 git push。

## 参考实现（读它们，复用骨架）

- `scripts/fcfy_study.py` / `scripts/fcfy_backtest.py`：Panel、NW t、trial 账本、
  成本模型、闸矩阵——评估骨架直接照搬，换信号源。
- `scripts/ipo_yield_study.py`：事件研究风格。
- `tests/test_fcfy_study.py` / `tests/test_fcfy_backtest.py`：fake fixture 风格。
- `quanti/backtest/overfit.py`：`deflated_sharpe_ratio`、`pbo_cscv`。

## 交付清单

1. `scripts/repurchase_event_study.py`（含 `--json` 账本输出）
2. `scripts/repurchase_bt.py`（若第一步过闸）
3. `tests/test_repurchase_study.py`（+ bt 测试，若第二步存在）
4. `docs/2026-09-13-repurchase-alpha.md`（外部证据表 + 本地数字 + 结论）
5. 过闸才写 `strategies/repurchase_alpha.py`
6. `/opt/data/quanti/.venv/bin/python -m pytest -q` 全绿（既有 ~1201 例不许挂）

完成后：两个 commit——①脚本/测试 ②研究文档（含实测数字）。不要 push。
