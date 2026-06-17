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

## Windows 环境清单

- **机器**:Win10/11 或 Server,常开,关睡眠/休眠/锁屏,网络稳定(建议云 Windows VPS);注意 RDP 断开可能影响 QMT GUI。
- **QMT**:装券商定制包 → 登录交易账户 → 切 miniQMT/极简模式 → 记下安装目录与 `userdata_mini` 路径。
- **xtquant**:随 QMT 安装,**版本与客户端配套**,勿乱 pip;确认它支持的 Python 版本(决定 bridge 用哪个解释器)。
- **模拟环境**:若券商 QMT 提供模拟交易,影子阶段先用它,不动真钱。

## 分阶段计划

| 阶段 | 内容 | 状态 |
|------|------|------|
| **① Broker 接口** | 抽 `quanti.execution.base.Broker` Protocol,runtime 依赖接口;PaperBroker 结构化实现。纯重构。 | ✅ 合并 (#12) |
| **② qmt-bridge + 只读冒烟** | Windows 上 `xtquant` 包成 HTTP;只读脚本 `XtQuantTrader.connect → 查资金/持仓`,确认能读到真实账户。**不下单**。 | ⬜ 待环境 |
| **③ QmtBroker** | 实现 `Broker`:`order_stock` 下单 + 成交/委托回调 + 持仓现金对账 + 部分成交/废单/撤单/幂等。 | ⬜ |
| **④ 实时行情 + 盘中离场** | `xtdata` 订阅实时 → `DataProvider` 加实时接口;agent 改盘中循环,止损/止盈改盘中触发。 | ⬜ |
| **⑤ 影子模式** | QmtBroker 与 PaperBroker 并行,核对成交/持仓差异,跑一段。 | ⬜ |
| **⑥ 小资金实盘** | 通过对账 + 监控告警后,小资金灰度放量。 | ⬜ |

## 复用(无需重写)

`RiskManager` 硬限 / T+1 / 佣金印花税模型 / 挂单→成交生命周期 / 止损止盈 —— 迁移时大多直接复用,QmtBroker 主要是把"模拟成交"换成"真下单 + 等回报 + 对账"。

## 待用户提供(进入阶段 ②前)

1. `xtquant` 实际可用的 Python 版本(定 bridge 解释器);
2. 券商名 + `userdata_mini` 路径;
3. 是否有 QMT 模拟交易环境(影子阶段用)。
