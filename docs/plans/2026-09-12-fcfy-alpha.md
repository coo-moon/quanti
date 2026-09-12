# 任务：自由现金流收益率（FcfY）截面 alpha 研究

## 背景（先读）

- 存活策略：`strategies/sse_index_enhance.py`（PR #197，结构性恒等式超额）。
  已证否清单见 docs/2026-07-01-can-this-system-make-money.md 与
  docs/2026-09-11-*.md：截面因子 composite（13 因子融合）、防御倾斜、ML 截面、
  择时、宽度、可转债、市场中性 OOS、红利TR增强、打新 overlay@百万级。
- **FcfY 不在证否清单**：原 13 因子是 EP/PB/动量/低波/规模等，**没有任何
  现金流口径因子**。这是「新数据接口带来的未测因子」——tushare `cashflow`
  接口数据（2026-09-12 已验证逐票可行）首次进库。
- 外部证据表（写进研究文档的动机一节）：

  | 来源 | 口径 | 结果 |
  |---|---|---|
  | Novy-Marx 2013 / AQR "Other Side of Value" | 美股 gross profitability (GP/A)，多空 | FF3 alpha +0.52%/月，t=4.49；10+ 国家国际证据 t≈4.5+ |
  | 国信证券 2025-04（港股） | FCF比率=FCF/EV TTM，FCF30 组合 vs 全指 | 23 年回测年化超额 +8%、alpha +9%；因子**单向性**（空端无效，long-only 可用） |
  | 广发金工 2025（A股） | 自由现金流率（国证/中证 FCF 指数口径） | 多头组合年化 >10%；FCF 指数 vs 中证红利：更高收益、更动态调仓 |
  | 学理 | FCF = 盈利质量（剥离应计）+ 价值（低估值）的合成 | Piotroski F-score 的现金端子项、HAJIME「Cashflow 是最难粉饰的报表」 |

  A 股注意：上述是**券商/指数公司口径**（自家 universe、含财务费用调整），
  我们要用自己的数据、自己的成本模型、DSR/PBO 闸重做一遍才算数。

## 数据层（已就绪，勿改）

`scripts/backfill_cashflow.py`（主编已写好并提交）从 tushare `cashflow` 逐票
回填 market.db 新表 `cashflow_items`：

    cashflow_items(code, end_date, ann_date, f_ann_date, update_flag,
                   n_cashflow_act, c_pay_acq_const_fiolta)
    -- 均为报告期 YTD 累计值(元)；ann_date=公告日(PIT 关键)；主键含
    -- (code,end_date,ann_date,update_flag)，同一报告期可能有更正报告多行。

**你执行时该表正在回填中**（全市场沪深 ~5400 票 @45/min ≈ 2h，进度文件
`/tmp/cf_progress.json`，完成标记 `/tmp/cf_done.json`）。开发期先写代码+
fake 测试；最后一步的正式全量回测**必须等 `/tmp/cf_done.json` 出现**再跑
（轮询等待，每 120s 一次，最长 180 分钟；超时则跑 --codes 800 冒烟并在文档
标注「正式数字待补跑」，把补跑命令写清楚）。

## 要做什么

### 第一步：因子构造与 IC 实证 `scripts/fcfy_study.py`

仿照 `scripts/earnings_revision_study.py` 的评估骨架（SQL PIT、rank-IC、
Newey-West t、`--json` 输出），因子：

1. **FCF TTM**（单位元）：对每票取 `ann_date ≤ D` 的最新可见报告版本
   （同 (code,end_date) 多版本按 ann_date 取最新；update_flag 保留原始值
   但去重键含它，防止「原报告+更正报告」并存时的重复——用 SQL 窗口函数
   按 (code, end_date) 分组取 ann_date 最大者）。累计值拼 TTM：
   - 年报(end_date 12-31)：TTM = 年报累计；
   - 中报/季/三季：TTM = 本期累计 + 上年年报累计 − 上年同期累计；
     上年年报或上年同期缺失则该行不可用（不许用 4×单季粗估）。
2. **FcfY = FCF_TTM / total_mv**（total_mv 来自 `daily_basic`，万元口径——
   **注意单位换算**：daily_basic.total_mv 是万元，FCF 是元）。
3. 变体（每个都算，最后如实报告多重性）：
   - FcfY_mv 主口径（市值分母）；
   - FcfY_ev = FCF_TTM / (total_mv + 有息负债−货币资金)？——**不做**，
     低积分 token 无资产负债表接口，任务书层面砍掉，文档说明原因；
   - CFOY = CFO_TTM / total_mv（不扣 capex，纯经营现金流收益率，作对照）。
4. 宇宙与清洗：沪深 A（daily_quotes 有行情的全部 code，含退市票——
   幸存者偏差本地库已修）；剔除行业字段含「银行」「保险」「证券」及
   券商口径不可比的金融票（industry LIKE '%银行%' 等）；剔除上市 <120
   交易日的次新；剔除 daily_basic 缺失（无市值不可算）；ST 判定用
   `name_history` 表 PIT 判（参考 backfill_dividends 的做法，绝不用今天
   的名字回判历史）。
5. 评估：调仓日 D ∈ 2022-01 ~ 数据末（本地库行情起点 2021-09-13），步长
   **20 交易日**（低换手结构性效应的正确尺度，5 日尺度是给动量用的），
   rank-IC vs 前视 20 日 hfq 收益（复权因子 reconstruct 口径已在库里），
   NW t（滞后 19）。对照：等权全市场中位。输出逐年 IC。

**门槛（先过才有第二步）**：全样本 |IC 均值| ≥ 0.03 且 NW |t| ≥ 2.5 且
**逐年符号一致**（2022-2026 至少 4/5 年同号）。达不到 → 直接写证否文档
（照 docs/2026-09-11-dividend-tr-enhance.md 格式），止步于此。

### 第二步：组合级回测 `scripts/fcfy_backtest.py`

过门槛后：FcfY 最高的 top-30 等权（FCF30 复刻口径），月频调仓（20 交易日
≈ 1 个月），全成本模型复用项目引擎口径（佣金万 2.5+下限 5、印花税分段、
过户费、平方根冲击滑点——参考 `scripts/sse_enhance_backtest.py` 与
quanti/backtest 的成本路径；如果引擎不适用于这种纯截面轮动，就用脚本级
向量化成本近似，**但成本假设必须与引擎一致并在文档列明**）。基准：
(a) 全市场等权（诚实基准 +10.49%/5y 同源），(b) 沪深300。
报告：年化超额、TE、IR、最大回撤、**年换手率**、逐年超额分解。

### 第三步：过拟合闸 `quanti/backtest/overfit.py`（现成的，别造轮子）

- DSR：trial_sharpes 传**你实际试过的全部变体 Sharpe**（第一步的 FcfY、
  CFOY ×  universe 变体 × 调仓步长 5/20/60，全部如实入账——多重检验账
  本诚实是闸有效的前提，参考项目里 DSR 拦截 3 个伪 edge 的先例）。
- PBO：CSCV 16 划分，perf matrix 用同一组 trial。
- **验收闸：DSR ≥ 0.95 且 PBO ≤ 0.2 且 年换手 ≤ 300% 且 逐年超额 ≥0 的
  年数 ≥ 4/5**。全过 → 才写 `strategies/fcfy_alpha.py`（signal=目标权重，
  selectable=False 同 sse 模式）+ 完整回测 JSON 落 data/（不提交）。
  任一不过 → 停在研究层，证否文档收尾。**证否文档就是有效交付**。

### 第四步：测试 `tests/test_fcfy_study.py`

全注入 fake（sqlite 临时库 + 手搓 cashflow_items/daily_basic/daily_quotes
行），零触网零真库。覆盖：TTM 拼接（年报/中报/三季/缺上年数据剔除）、
多版本去重取最新 ann_date、PIT（ann_date > D 的报告绝不可见）、单位
换算（万元 vs 元）、rank-IC 手算对账、逐年符号统计、金融票剔除、
ST PIT 判定。参考 tests/test_tushare_adapter.py / test_ipo_yield_study.py
的 fixture 风格。

### 第五步：研究文档 `docs/2026-09-12-fcfy-alpha.md`

结论诚实回答：(a) FcfY 在 A 股是否独立于已证否的 13 因子 composite 有效
（做一个增量检验：FcfY 对现有 composite score 正交化后的残差 IC，若残差
归零说明是旧因子的换皮——数据在 generated_factors/factor_ic_history 或
重算，拿不到就明说拿不到）；(b) 单向性是否成立（bottom 分位是否跑输）；
(c) 若过闸：现实预期（年超额、最差年、容量粗估——top30 等权 100 万资金
的冲击成本占比）；若证否：死因是 IC 弱、还是成本吃光、还是闸不过。
外部证据表（背景节的）进文档，与本地实测数字并排对照，说明差异。

## 约束（禁止事项）

- **前视是死刑**：任何在 D 日使用的报表必须 ann_date ≤ D（SQL 层强制，
  不许「先全取再过滤日期列」时把 f_ann_date/重述日期搞混）。TTM 拼接
  用到的上年年报同样要求其在 D 已公告（上年年报 4 月底才披露完——
  3 月份调仓日用的是**上上年**年报 + 上年三季报拼 TTM，这条要测到）。
- 不许用今天的 universe 回判历史（退市票必须在样本里，本地库有）。
- 测试不碰网络、不碰 /opt/data/quanti/data/market.db（只读副本都不许，
  用临时 sqlite）。研究脚本读 market.db 用 `file:...?mode=ro` 只读 URI。
- 不动 strategies/sse_index_enhance.py、dividend_tr_enhance.py 及任何
  生产热路径；不动 quanti/backtest/overfit.py 的闸值。
- 回填脚本 scripts/backfill_cashflow.py 已完成，勿改逻辑；若发现字段
  口径问题在文档里记录，不要自行改表结构。
- 中文 docstring 风格与现有策略/脚本一致；新增 pytest 全绿、ruff 干净
  （ruff 配置见 pyproject.toml）。python 一律用 /opt/data/quanti/.venv/bin/python。

## 完成定义

研究脚本 + 测试 + 研究文档（过闸则另有策略文件 + 回测 JSON）。回测 JSON
落 data/（.gitignore 内，不提交）。**若结论是否定，证否文档就是交付物**——
照 docs/2026-09-11-dividend-tr-enhance.md 的格式写清楚为什么死。
不要 commit/push（编排者负责验收后收尾）。
