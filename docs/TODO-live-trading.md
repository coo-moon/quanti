# 实盘接入 TODO

**当前状态**：使用 PaperBroker 模拟盘运行。
**决定时间**：2026-05-19。
**计划复盘日**：2026-08-19（满 3 个月）。
**前置条件**：PaperBroker 在 3 个月内**累计年化 ≥ 目标 70%** 且**最大回撤未触发 -20%** 才考虑接实盘；否则继续模拟。

---

## 1. 通过 PaperBroker 验证（接实盘前必须达成）

- [ ] 每周看一次 `/api/agent/decisions`，确认 Agent 周期能正常完成
- [ ] 3 个月内 `portfolio_snapshots` 表里至少有 60 个交易日数据
- [ ] 净值 ≥ 初始 × (1 + 目标年化 × 90/365 × 0.7) 才算通过
- [ ] 最大回撤未跌破 -20%（看 `quanti agent status` 或 Web 页面）
- [ ] 至少触发过一次 `risk_reject`、一次 `stop_loss` —— 风控链路真的在工作
- [ ] 至少经历一次 `strategy_pick` 切换 —— Selector 有在动态调整

---

## 2. 实盘券商选型（满 3 个月后再做）

候选：

| 券商 | 资产门槛 | 推荐指数 | 备注 |
|------|---------|---------|------|
| 华鑫证券 | 50 万 | ★★★★★ | 对量化用户最友好 |
| 国金证券 | 50 万 | ★★★★ | 资料最多，社区成熟 |
| 国元证券 | 50 万 | ★★★ | 稳定但流程繁 |
| 东方财富 | 50 万 | ★★★ | 用 EMC 而非 QMT |

低门槛备选（如果不满 50 万）：
- **EasyTrader** + 任意券商客户端（模拟点击 PC 客户端）
- **雪球模拟盘**（注册即可，纯模拟但有真实行情）

---

## 3. 用户侧操作（只有本人能做）

- [ ] 选定一家券商，**线上或线下开户**（已有账户可跳过）
- [ ] 在 App 内申请"量化交易权限" / "QMT 接入"
- [ ] 提交资产证明（账户内 ≥50 万持仓或现金）
- [ ] 等待审核（典型 3~10 个工作日）
- [ ] 收到 QMT 客户端下载链接 + 资金账号 / 密码 / mini-QMT 安装路径

---

## 4. 部署环境（重要：当前 Mac 跑不了）

QMT 客户端**只有 Windows 版**，没有 Mac/Linux 版。三选一：

- [ ] 选项 A：一台长开的 Windows 小主机（推荐，~2000~4000 元）
- [ ] 选项 B：Parallels / VMware 跑 Windows 虚拟机（开发用 ok，长开不便）
- [ ] 选项 C：云上 Windows 实例（阿里云/腾讯云 ECS，~80~150 元/月）

无论哪个，机器需要：
- Windows 10/11
- 安装券商提供的 QMT 客户端
- 安装 Python 3.11+
- `pip install xtquant`
- 把 Quanti 部署上去，通过 `quanti up` 启动

---

## 5. 代码侧需要做的事

**新增**：

- [ ] `quanti/execution/qmt_broker.py`，实现 `QmtBroker` 类，接口与 `PaperBroker` 完全一致（duck typing）：
  - `execute_signal(signal, strategy_name) -> bool`
  - `execute_signals(signals, strategy_name) -> BrokerResult`
  - `snapshot_portfolio() -> dict`
  - `check_stop_loss() -> int`
- [ ] 内部用 `xtquant.xttrader` 下委托、查持仓、查资金
- [ ] 持仓和组合状态**双写**：QMT 真实数据 + 本地 DB 镜像（DB 仍是事实记录，崩了能恢复）
- [ ] 处理委托回报异步回调（QMT 是回调式 API，不是同步成交）

**改造**：

- [ ] `quanti/api/app.py` 加一个开关：`live_trading: bool = False`，默认 False → PaperBroker，True → QmtBroker
- [ ] `quanti/cli.py` 的 `quanti up` 加 `--live` flag，需要同时传 `--broker qmt`
- [ ] `quanti up --live` 启动时必须二次确认：交互式输入 `I_KNOW_REAL_MONEY` 才生效

**风控加固**（实盘必加）：

- [ ] 单日最大下单笔数硬上限（默认 20，可在 `RiskConfig` 调）
- [ ] 单日最大成交金额硬上限（默认账户净值的 30%）
- [ ] 单笔下单金额硬上限（默认账户净值的 10%）
- [ ] 价格偏离保护：限价单不能偏离当前价 ±3%
- [ ] 涨跌停板检查（A 股 ±10%、ST ±5%、创业板 ±20%）
- [ ] 退市/停牌检查
- [ ] Agent 在实盘模式下默认 `enabled=False`，必须手动 `agent_start` 才会跑

**新测试**：

- [ ] `tests/test_qmt_broker.py`，用 mock `xtquant` 验证下单 / 撤单 / 查持仓的对接逻辑
- [ ] `tests/test_live_safety.py`，验证所有硬上限都生效

---

## 6. 切换流程（满 3 个月评估通过后）

1. [ ] 在 Windows 机器上跑 `quanti up --no-agent`（仍走 PaperBroker），确认 Web 能开
2. [ ] 安装好 QMT 客户端并登录券商账户，起好 `bridge/qmt_bridge.py` 并确认 `/health` 的 `mode∈{xt,vnpy} && trader_connected && datafeed_ok`（直连后端 `mode==xt`，见下节 6b）
3. [ ] 切实盘用**环境变量**（不是 CLI flag）：`QUANTI_ACCOUNT=live QUANTI_LIVE_ACK=I_KNOW_REAL_MONEY quanti up`；实盘默认**不自动拉起 Agent**（须手动 `agent_start`），故这一步只起服务
4. [ ] 用 Web "手动下单" 测一笔 100 股最便宜的标的，确认下单/成交/撤单全流程
5. [ ] 调小目标资金（比如先只放 5 万到这个账户），观察一周
6. [ ] 在 Web 上 `agent_start`，观察一周
7. [ ] 满意后再把目标资金加上去

---

## 6b. QMT bridge 启动（直连 xtquant 后端，已实测 2026-07-08 江海 QMT）

`bridge/qmt_bridge.py` 有两个 live 后端，**推荐直连 xtquant（`mode==xt`）**——它直接用 miniQMT 自带的
`xtquant`（xttrader 下单 + xtdata 取数），避开了 vnpy_xt 对 miniQMT 的三处不兼容（连接键错、把交易路径写成
`\userdata` 而非 `userdata_mini`、`仿真交易` flag 语义暧昧）。已实测：PyPI 版 `xtquant`(250516) 在 Python 3.13
上对江海 miniQMT 的数据与交易通道都连通（`connect()->0`、`query_account_infos`/`query_stock_asset` 均返回真实账户）。

**前置**：交易账户必须已**登录进 QMT 交易端**（`XtMiniQmt.exe`）。否则 `/health` 的 `trader_connected` 为
`false`（客户端日志会报 `accountInfos not found` / `[] not in datacenter`），这不是代码问题，登录即恢复。

**装依赖**（bridge 专用环境，任意 Python 3.10+，与 quanti 主环境隔离）：

```bash
python -m venv qmt-bridge-venv
qmt-bridge-venv/Scripts/pip install xtquant   # 或 vnpy vnpy_xt（会带上 xtquant）
```

**启动**（环境变量驱动，见 `bridge/qmt_bridge.py:_make_live_backend`）：

```bash
QMT_BRIDGE_BACKEND=direct \
QMT_ACCOUNT=<资金账号> \
QMT_USERDATA_MINI="<...>\江海证券QMT实盘_交易\userdata_mini" \
qmt-bridge-venv/Scripts/python bridge/qmt_bridge.py --host 127.0.0.1 --port 18099
```

- `QMT_ACCOUNT_TYPE` 可选，默认 `STOCK`。
- **下单默认关闭**：`submit_order` 默认拒单（`bridge orders disabled`），只放行查询/快照。确认整链无误、评估通过后，再加
  `QMT_BRIDGE_ALLOW_ORDERS=1` 才允许真实下单——这是观察期的安全闸。
- 起来后确认 `curl http://127.0.0.1:18099/health` 返回 `mode==xt && trader_connected==true && datafeed_ok==true`
  （bridge 每 10s 心跳查一次资金，空闲期也保持 `datafeed_ok` 新鲜）。
- quanti 端照常 `QUANTI_ACCOUNT=live QUANTI_LIVE_ACK=I_KNOW_REAL_MONEY quanti up` 即通过 `make_broker` 走
  `QmtBroker(require_live=True)` 连这个 bridge。

---

## 7. 紧急回滚

任何时候出问题：

1. [ ] Web → 停止 Agent 按钮
2. [ ] `quanti agent stop` 命令
3. [ ] MCP `agent_stop` 工具
4. [ ] 直接关 QMT 客户端，券商账户回到手动状态
5. [ ] DB 里所有持仓与订单都有完整记录，可以回放排查

---

## 8. 不接 QMT 的备选路线

如果到时候发现 QMT 流程太重，可以考虑：

- [ ] EasyTrader：开源 Python 库，模拟点击券商 PC 客户端，门槛低但易碎
- [ ] 雪球模拟盘 API：纯模拟，但比 PaperBroker 多一层"真行情"验证
- [ ] 同花顺 iFinD：5 万门槛，但接口稳定
