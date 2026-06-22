# 实盘接入(QMT / miniQMT)— 路线图

**Date**: 2026-06-16
**Owner**: wenbo
**Status**: 进行中 — 第 1 步(Broker 接口)已合并 (#12);其余待 Windows 环境就绪
**一句话**: 把 quanti 从纯模拟盘(PaperBroker)接到 A 股实盘,通道选 **QMT 的 miniQMT(`xtquant`)**——它一个 SDK 同时解决"真实下单"和"实时行情"两个最大缺口;用**桥接架构**隔离 Python 版本,分阶段、影子先行、小资金验证后再放量。

## 背景

quanti 现状:rules-first 内核(走查选股 + 截面因子 + ensemble + `RiskManager` 硬限 + 止损/止盈)+ 可选 LLM 判断层,执行走 `PaperBroker`(模拟 T+1 / 涨跌停 / 费用 / 挂单生命周期)。要接实盘,三个结构性缺口:

1. **没有真实券商适配器** —— `quanti/execution/` 只有 `PaperBroker`;
2. **持仓/现金是本地模拟** —— 实盘必须从券商账户对账,券商是唯一真相源;
3. **数据是 EOD 日线** —— 没有实时行情,盘中止损/止盈失效(只在收盘算)。

通道选型:个人 A 股不能直连交易所。QMT 的 **miniQMT** 暴露 `xtquant`(`xttrader` 下单/查询/回调,`xtdata` 实时行情),比 easytrader(GUI 模拟点击)正规、稳定,且一并解决数据缺口。**前提**:券商已开通 miniQMT 权限(✅ 已确认)+ 程序化交易报备(2024 起强制,经券商办)。

## 红线(沿用模拟盘,不可触碰)

1. **`RiskManager` 硬限是地板**:单股 10% / 行业 30% / 总仓 80% / -8% 止损 / 风控成交时复检——实盘 broker 同样在下单前过这一关,不可绕过。
2. **影子先行**:真实 broker 先 dry-run / 模拟盘,与 PaperBroker 并行跑、对账一致,才放真钱。
3. **小资金起步** + Kill switch + 人工确认门 + 对账告警。
4. **本地 DB 降级为镜像**:实盘下持仓/现金以券商查询为准,本地只缓存 + 对账,冲突以券商为准。

## 架构:桥接(隔离 Python 版本)

`xtquant` 跟 QMT 客户端配套,支持的 Python 版本大概率 ≠ quanti 的 3.14。故拆成两进程,localhost 通信:

```
Windows 机器(常开、交易时段保持登录):
├── QMT 客户端          登录交易账户 + 切 miniQMT 模式
├── qmt-bridge          用 xtquant 支持的 Python(独立 venv),import xtquant,
│                       把 下单/查持仓资金/成交回调/实时行情 包成 localhost HTTP
└── quanti              自己的 venv(3.14);QmtBroker 通过 localhost 调 qmt-bridge,
                        实现 quanti.execution.base.Broker 接口
```

起步:两进程同机走 localhost,最简单。后期可把 quanti 主体挪回 Mac、只留 bridge 在 Windows。

### bridge 后端选型:Option B —— 用 vnpy 的 QMT gateway(已落地, #22)

调研后(见 `docs/2026-06-20-reference-mature-quant-systems.md`)决定:**不在 bridge 里手写 xtquant 的订单状态机/异步成交/重连**,而是在 bridge 进程内跑 **headless vnpy + `vnpy_xt` gateway**,把其事件翻译成现有 HTTP 契约。

- **为什么保留 bridge 进程(而非进程内直接用 vnpy)**:故障隔离(QMT 崩了不带崩 web/agent)、不把 quanti 主体绑死在 vnpy+Windows、保留已建好且测试过的 `QmtBroker`/契约。
- **Python 版本**:vnpy 支持 3.10–3.13(推荐 3.13),quanti 正是 3.13 —— 若 xtquant 也能在 3.13 跑,bridge 与 quanti *可* 共用解释器;但仍保留进程边界做故障隔离。
- **落地**:`bridge/vnpy_backend.py`(`VnpyBackend`,守卫导入,无 vnpy 退回 mock)+ `bridge/qmt_bridge.py` 检测/委托。交易走 vnpy gateway 回调,行情走 xtdata;可卖量取 `PositionData.yd_volume`(T+1)。quanti 侧零改动。

## Windows 环境清单

- **机器**:Win10/11 或 Server,常开,关睡眠/休眠/锁屏,网络稳定(建议云 Windows VPS);注意 RDP 断开可能影响 QMT GUI。
- **QMT**:装券商定制包 → 登录交易账户 → 切 miniQMT/极简模式 → 记下安装目录与 `userdata_mini` 路径。
- **xtquant**:随 QMT 安装,**版本与客户端配套**,勿乱 pip;确认它支持的 Python 版本(决定 bridge 用哪个解释器)。
- **模拟环境**:若券商 QMT 提供模拟交易,影子阶段先用它,不动真钱。

## 数据源分工(决策)

按**用途**分,不是单一源全替换。原则:研究/回测链路**不被 QMT 在线绑死**,实时只服务执行与盘中风控。

| 用途 | 源 | 说明 |
|------|------|------|
| 选股 / 因子 / 回测 / regime / 研究 | **日线** | 这些全是**日线粒度**,不需要分钟数据 |
| 历史日线主源(交易宇宙) | **xtdata → 缓存进 SQLite** | 券商级、与成交一致;写**原始价(不复权)+ adj_factor**(=后复权/原始),与 akshare/tushare 同一口径、同一个 `save_daily_quotes` 出口;研究/回测读取时由 `DataProvider` 复权(hfq),实盘下单用原始价 |
| 兜底 + 新闻(情绪层) | **akshare**(保留) | 免费、覆盖广;xtdata 没有新闻 |
| 实盘下单 + 盘中止损/止盈 | **xtdata 实时报价**(tick/快照) | 注意:要的是**实时报价,不是分钟 K 线**;分钟 bar 仅在将来写日内策略时才需要 |
| 退市股 / point-in-time(可信回测) | **Tushare Pro / 米筐 / 聚宽**(按需) | xtdata 与 akshare **都给不了**——这是数据**内容**问题(幸存者偏差/前视偏差),不是存储/搬运问题 |

要点:
- **xtdata 缓存进 SQLite** 是关键设计——研究/回测读 SQLite,只有**同步那一下**需要 QMT 在线(实盘期间 QMT 本来就开着,顺手搭车)。等于用一个源**统一历史与实时**,避免 akshare 历史 + xtdata 实时的复权/口径不一致。
- xtdata 缓存**不修偏差**:它给的是当前在市标的的 bar,没有退市股历史、没有 point-in-time 基本面。真要回测可信,单独上专业源,与本路线图解耦。
- 初始全量下载较重(5519 只逐只),且历史深度受券商限制;增量后很轻。

### 实盘 / 模拟盘的数据源关系(分库后)

分库(已落地)后,paper.db / live.db 各管各的**交易状态**,但**ATTACH 同一个 `data/market.db`**。所以两账户的数据源关系是:

| 数据 | 模拟盘(paper.db) | 实盘(live.db) | 是否同源 |
|------|------|------|------|
| 历史日线(选股/因子/回测/regime) | 共享 `market.db` | 共享 `market.db`(同一文件) | **同源,且应当同源** |
| 实时报价(执行 + 盘中止损/止盈) | 不用(按日线模拟次日开盘成交) | xtdata 实时 | 实盘独有 |
| 新闻(情绪层) | market.db 的 news_sentiment | 同上 | 同源 |

- **历史必须同源**:实盘要交易的,正是回测/选股在**同一份历史**上验证过的策略;两账户用不同日线会让 backtest≡live 脱节。所以分库**只分交易状态,行情共享一份**——"钱分开,看的行情不分开"。
- **实时是实盘独有的一层**:模拟盘不接实时,用日线模拟成交;实盘叠加 xtdata 实时做盘中执行与风控。
- 终态:`market.db` 的日线主源从 akshare 换成 xtdata 缓存后,两账户仍共享这一份(依然同源),实盘只是在其上多订阅一路实时。
- 写入一致性:`market.db` 的日线只由**一个**写入方维护(bg-sync,akshare→将来 xtdata),不让两个源对同一根 bar 互相覆盖。

## 分阶段计划

| 阶段 | 内容 | 状态 |
|------|------|------|
| **① Broker 接口** | 抽 `quanti.execution.base.Broker` Protocol,runtime 依赖接口;PaperBroker 结构化实现。后续补急停/健康/回撤熔断。 | ✅ 合并 (#12,#19) |
| **② bridge + 只读冒烟** | bridge(HTTP 契约 + mock)就绪、可端到端跑;实盘改为 **vnpy_xt** 后端。**只读 spike 待 QMT 机器**:headless vnpy + vnpy_xt `connect → 查资金/持仓`。 | 🟡 脚手架就绪 (#19,#22),待真机 |
| **③ QmtBroker** | 实现 `Broker`:下单 + 成交/委托回调 + 持仓现金对账 + 部分成交/废单/撤单。**已审计加固**(风控前置、T+1 可卖量、对账);实盘下单经 vnpy gateway。 | 🟡 已实现 (#19,#22),待真机验证 |
| **④ xtdata→SQLite 历史源** | `XtdataAdapter` 经 bridge 取日线落 `daily_quotes`(同 akshare 出口);vnpy 后端用 xtdata 取真数据。 | 🟡 骨架就绪 (#19,#22),待真机 |
| **⑤ 实时报价 + 盘中离场** | `xtdata` 订阅实时报价 → `DataProvider` 加实时接口;agent 改盘中循环,止损/止盈改盘中触发(注意:实时报价非分钟 bar)。 | ⬜ |
| **⑥ 影子模式** | QmtBroker 与 PaperBroker 并行,核对成交/持仓差异,跑一段。 | ⬜ |
| **⑦ 小资金实盘** | 通过对账 + 监控告警后,小资金灰度放量。 | ⬜ |
| (可选) 偏差修正 | 若要可信回测,接 Tushare/米筐 补退市股 + point-in-time。与上面解耦,按需做。 | ⬜ |

> 另:交易核心已做对抗式审计 + 修复(前视/回测≡实盘、行业·单票·总仓硬限、组合 -15% 熔断、日内上限、选股指标),并修了 DB 跨线程脏读。见 `docs/2026-06-20-audit-trading-core.md`。

## 复用(无需重写)

`RiskManager` 硬限 / T+1 / 佣金印花税模型 / 挂单→成交生命周期 / 止损止盈 —— 迁移时大多直接复用,QmtBroker 主要是把"模拟成交"换成"真下单 + 等回报 + 对账"。

## Option B 待真机验证清单(代码中以 `# VERIFY` 标注)

`bridge/vnpy_backend.py` 在没有 vnpy/xtquant 的机器上只能按文档/惯例写"最佳猜测",以下三处**必须在装好 `vnpy_xt` 的 QMT 机器上核对**(只读 spike 阶段即可全部确认,无需下单):

1. **connect 设置键**:`vnpy_xt` gateway `connect()` 接受的 setting dict 字段名/取值(账号、`userdata_mini` 路径、账户类型 STOCK 等)—— 以 gateway 源码 / `get_default_setting()` 为准,据此修正 `_connect_setting_from_env()` 的 env→key 映射。
2. **股票现货的 offset 语义**:A 股现货 买=开、卖=平 在 vnpy 里如何表达(`Offset.OPEN`/`Offset.CLOSE` vs `Offset.NONE`)—— 不同 gateway 口径不同,下单前必须确认,否则平仓单可能被拒或语义错。
3. **symbol 格式**:vnpy 的 `vt_symbol` / `symbol.exchange`(如 `000001.SZSE` / `600519.SSE`)与 quanti 内部 `code` 的双向映射;同时核对 xtdata 行情接口的代码后缀(`.SZ` / `.SH`)。

附:`PositionData.yd_volume` 作为 T+1 可卖量、异步成交回调(`on_order` / `on_trade`)累计的字段名,也在真机上顺带验证。
- **protections 依赖交易日历**：StoplossGuard/MaxDrawdown 的窗口与锁期按交易日计；上实盘前确保 `trade_calendar` 已 sync（否则 `is_trading_day` 退化为"工作日"，窗口会偏）。

## 待用户提供(进入阶段 ② 只读 spike 前)

1. QMT 机器上 `xtquant` 实际可用的 Python 版本(确认能否用 3.13;定 bridge 解释器);
2. 券商名 + `userdata_mini` 路径 + 资金账号;
3. 是否有 QMT 模拟交易环境(影子阶段用);
4. 在该机装 `vnpy` + `vnpy_xt`,跑只读 spike 核对上面三条 `# VERIFY`。
