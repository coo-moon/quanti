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
  由主编写好并提交，你执行时回填已在跑（进度 `/tmp/rp_progress.json`，
  完成标记 `/tmp/rp_done.json`）。

## 动机：外部证据表（写进研究文档，与本地实测并排对照）

| 来源 | 口径 | 结果 |
|---|---|---|
| Ikenberry, Lakonishok & Vermaelen 1995 (JFE, 被引 3100+) | 美股开放市场回购公告后 4 年买入持有 | 平均异常收益 +12.1%；价值股组年化 +12.1%（9.2%/组另计）；市场反应不完全=漂移 |
| 湖大报社科版 2021（A股 2011-2019，316 事件，市场模型） | 回购公告 CAR[-1,1] | 均值 +2%，t=5.46，1% 显著 |
| 中金 2025「十问十答：A股事件驱动」 | 事件研究法 CAR 窗口框架 | 事件 alpha 在 A 股可测、需按 T+1 可交易口径对齐 |
| 证券时报 2026-08-05（万得口径复盘） | Wind 股票回购指数（近 6 个月发市值管理回购的成份股）2018-07 至今 | 累计 +110% vs 上证 <40%；2021-2026 每年跑赢上证 |
| 源达证券 2025-03（增持类比） | 首次增持公告后 90 日 vs 万得全A | +3.4%；<50 亿市值组 +7.0%（1%+ 大比例组 30 日 +2.92%） |
| 证券时报 2026-08（同一篇） | 停止回购事件 | 金风科技终止后 5 日 -7%，终止公司累计跌幅均 >10% ⇒ 预案有「画饼」尾部风险 |

为什么不能直接搬：以上是指数公司/自家样本口径；预案≠落地（A 股「重预案轻落地」
问题真实存在）；漂移文献是**长期**收益（1-4 年），T+1 散户可交易的中短期窗口
（20-60 日）证据只有事件研究 CAR。用自己的数据、自己的成本模型、DSR/PBO 闸
重做才算数。

## 数据层（已在回填，勿改）

`scripts/backfill_repurchases.py` 逐月拉 tushare `repurchase`（2018-01 起，
自动处理单次 2000 行截断→按旬细分），落 market.db 新表：

    repurchase_events(code, ann_date, end_date, proc, exp_date,
                      vol, amount, high_limit, low_limit)
    -- proc ∈ {预案, 股东大会通过, 实施, 完成, 停止}
    -- ann_date=公告日(YYYY-MM-DD, PIT 关键)；amount=元：预案行为拟回购金额、
    --    完成行为已回购金额；exp_date=预案到期日（仅预案行有值）

## 要做什么

### 第一步：事件研究 `scripts/repurchase_event_study.py`

评估窗 = 本地行情覆盖（daily_quotes 2021-09-13 起 ⇒ 事件 ann_date ∈
[2021-10-01, 2026-09-10]；行情末 2026-09-10，长窗按可得 bar 截断并如实报 n）。

1. **事件定义（主口径）**：`proc='预案'` 行。同一 (code, ann_date) 若有多行按
   (code, ann_date) 去重取一条；同票多次预案算独立事件（组合层再处理重叠）。
   辅助口径（全部入账为 trial）：`proc='股东大会通过'`、`proc='完成'`（首次）。
2. **T+1 可交易口径（前视红线）**：事件参考日 D0 = **晚于** ann_date 的第一个
   交易日（announcement 盘后发布是常态，ann_date 当天收盘不可得）。CAR 从 D0
   **开盘**起算：CAR(t1,t2) = Σ hfq 日收益（D0 开盘买入→D0+t1 收盘…的精确口径：
   用 close-to-close 序列，首日收益 = close(D0)/open(D0) − 1，之后全收对收）。
   基准 = 同期全市场等权（daily_quotes 全票日收益均值，与 fcfy 研究同源）。
3. **窗口**：(+1,+5)、(+1,+20)、(+1,+60)（含 D0 日）；NW t（重叠修正）；
   逐年（2022-2026）均值、胜率（CAR 末值 >0 占比）、事件数。
4. **清洗**（与 fcfy_study 同源）：剔金融（industry LIKE 银行/保险/证券）；
   剔上市 <120 交易日次新（用 daily_quotes 首行日期）；剔 ann_date 后无行情
   （停牌至数据末）；ST 用 name_history PIT 判定（公告日时点）；退市票保留。
5. **预声明的切片（都是 trial，进 DSR 账本）**：
   - 规模三档（公告日 total_mv  tercile）；
   - 预案力度两档：amount_low / total_mv ≥ 0.1% vs < 0.1%（amount 为拟回购
     金额下限，单位元，daily_basic.total_mv 是**万元**，换算！）；
   - 三种 proc 口径。
6. **门槛（先过才有第二步）**：主口径 CAR(+1,+20) 均值 >0 且 NW t ≥ 2.5 且
   胜率 ≥ 55% 且 2022-2026 逐年均值 ≥4/5 年为正。主口径不过但某个预声明切片过
   ⇒ 允许进第二步（该切片为主口径，账本如实记全部多重性）；全灭 ⇒ 证否文档收尾。

### 第二步：组合级回测 `scripts/repurchase_bt.py`

过门槛后，复刻「事件篮子」可交易形态：

- 信号：每 20 交易日调仓日 D，候选 = 过去 20 个交易日内（ann_date ∈ (D−20, D]）
  发布**新预案**且未「停止」、按主口径（或过闸切片）入池的票；按 力度
  (amount_low/total_mv) 降序取 top-30，等权买入（次日 open 成交）；每票最长持有
  60 交易日或至下一个调仓日（先到者），卖出同按 open。
- 成本：向量化近似，与 fcfy_backtest 同源（佣金万 2.5+5 元下限、印花税分段、
  过户费、冲击 5bp+5bp√参与率、红利税三档按 dv_ratio 近似）；100 万资金。
- 基准：全市场等权（主）+ 沪深300（参考，若有本地指数序列）。
- 报告：年化超额、TE、IR、最大回撤、年单边换手、逐年超额分解、平均持仓票数
  （若某期候选 <5 只如实标注——2021-2023 事件稀疏期组合层数字没意义）。

### 第三步：过拟合闸（`quanti/backtest/overfit.py`，现成的别造轮子）

- DSR trial_sharpes = 全部实测变体（3 proc × 3 窗口 × 预声明切片 + 组合层网格），
  PBO 用同一组 trial 的 perf matrix。**账本诚实是闸有效的前提**。
- **验收闸：DSR ≥ 0.95 且 PBO ≤ 0.2 且 年单边换手 ≤ 300% 且 逐年超额 ≥0 年数
  ≥ 4/5（2022-2026）**。全过 → 写 `strategies/repurchase_alpha.py`（signal=目标
  权重，selectable=False 同 sse 模式）+ 回测 JSON 落 data/（不提交）。
  任一不过 → 停在研究层，证否文档收尾。**证否文档就是有效交付。**

### 第四步：测试 `tests/test_repurchase_study.py`、`tests/test_repurchase_bt.py`

全注入 fake（临时 sqlite：手搓 trade_calendar/daily_quotes/daily_basic/
repurchase_events 行），零触网零真库。覆盖：D0=晚于 ann_date 首个交易日
（周末公告/节假日公告用例）、D0 开盘起算的 CAR 手算对账、(code,ann_date) 去重、
多次预案独立事件、PIT（ann_date 晚于窗口的行不可见）、万元/元换算、2000 行
截断细分逻辑、金融/ST/次新剔除、组合层「新预案入池 + 停止剔除 + 60 日上限退出」。

### 第五步：研究文档 `docs/2026-09-13-repurchase-alpha.md`

结论诚实回答：(a) 预案/股东大会/完成三个时点哪个有信息、市场是否已在 D0 内
充分定价（对比中金「多数事件 T+1 内定价完成」）；(b) 漂移是否随时间衰减
（2021-2023 vs 2024-2026 两段对比——回购热是 2024 年后的事，拥挤度）；
(c) 停止回购事件的负 CAR 是否值得做成**排除项**（组合层剔除近 12 个月有停止
记录的票）；(d) 若证否：死因是漂移不存在、成本吃光还是闸不过。外部证据表与本
地数字并排对照说明差异。

## 数据等待纪律

正式全量回测**必须等 `/tmp/rp_done.json` 出现**（轮询每 60s，最长 90 分钟）。
超时则以已完成部分跑并在文档标注「正式数字待补跑」，把补跑命令写清楚。开发期
先用已回填的子集写代码 + fake 测试。

## 约束（禁止事项）

- **前视是死刑**：事件池构建只能用 ann_date ≤ D 的行；CAR 窗口从 D0 开盘起算，
  任何含 ann_date 当天及之前的收益都不许计入。切片阈值（0.1%、tercile 分界）
  用公告日当时的 total_mv，不许用今天的市值回判。
- 闸值不得自行放宽；DSR 账本不得只报过闸变体。
- 不修改 `scripts/backfill_repurchases.py`、不动 market.db 既有表结构；
  repurchase_events 由回填进程写入，研究层只读。
- 不碰 `strategies/`、`quanti/` 既有文件（除非过闸后新增 repurchase_alpha.py）。
- 测试不许触网（tushare/akshare import 都不许出现在被测试路径的运行时）。
- python 用 `/opt/data/quanti/.venv/bin/python`；market.db 只读引用
  `/opt/data/quanti/data/market.db`；不要跑 `quanti sync`。

## 参考实现（读它们，复用骨架）

- `scripts/fcfy_study.py` / `scripts/fcfy_backtest.py`：Panel、NW t、trial 账本、
  成本模型、闸矩阵——直接照搬评估骨架，换信号源。
- `scripts/ipo_yield_study.py`：事件驱动口径与限速回填风格。
- `tests/test_fcfy_study.py`：fake fixture 风格。
- `quanti/backtest/overfit.py`：`deflated_sharpe_ratio`、`pbo_cscv`。

## 交付清单

1. `scripts/repurchase_event_study.py`（含 `--json` 账本输出）
2. `scripts/repurchase_bt.py`（若第一步过闸）
3. `tests/test_repurchase_study.py`（+ bt 测试，若第二步存在）
4. `docs/2026-09-13-repurchase-alpha.md`（外部证据表 + 本地数字 + 结论）
5. 过闸才写 `strategies/repurchase_alpha.py`
6. 全部 pytest 通过（既有 1201 例不许挂）

提交：分两个 commit——①数据/脚本/测试 ②研究文档（含实测数字）。
