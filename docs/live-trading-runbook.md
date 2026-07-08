# 启用实盘说明书（QMT 直连）

> 本文是把 quanti 从**模拟盘（PaperBroker）**切到**真实券商实盘（QMT 直连 xtquant）**的**操作手册 + 前置条件 + 风险边界**。
> 面向的是「已经跑了一段模拟盘、准备上真钱」的时刻。**按本文从上到下走，任一前置条件不满足就停。**
>
> 相关文档：接入待办与业务前置见 [`TODO-live-trading.md`](TODO-live-trading.md)；就绪度审计见 [`2026-06-22-live-readiness-audit.md`](2026-06-22-live-readiness-audit.md)。

---

## 0. 当前状态（截至 2026-07-08）

- **运行中**：模拟盘 PaperBroker。
- **已就绪**：QMT 直连 xtquant 桥（`bridge/xt_direct_backend.py`，PR #126）——真机已实测**只读**链路端到端连通（江海证券 QMT 实盘，账户 85530137）：`/health` 报 `mode=xt && trader_connected && datafeed_ok`，能读到真实资金/持仓，quanti `make_broker(live)` 的 `QmtBroker.is_connected()` 为 True。
- **代码债已清零**：接实盘代码债 PR #125–#134 **全部修复**（见第 7 节，§7.2 全 ✅）。
- **UI 下单开关已就绪**（PR #136）：实盘账户下 Web「实盘控制」卡有一道**布防/撤防**开关（DB 持久化 `live_control.orders_armed`，**默认撤防=观察**），是真钱买入的最后一道、也是唯一能在 UI 里**实时切换**的闸。只拦买入；卖出/止损/清仓永不受影响。撤防即一键在应用内停掉所有新买入（见阶段 C/D 与第 5 节）。
- **待做（真机下单冒烟，已计划）**：真实**下单**从未在真机跑过（下单闸默认关闭）；冒烟测试**已计划、待盘中 + 操作者在场**按阶段 D 执行——这是上真钱前唯一剩下的技术验证步骤。
- **未达标（业务跑道）**：见第 1 节（模拟盘连续交易日 + 风控触发尚未满足）。

一句话：**技术链路通了、安全硬化已全部落地，「可以放真钱」只差业务跑道验证和一次真机下单冒烟。**

---

## 1. 前置条件（必须全部 ✅ 才可进入第 3 节）

### 1.1 业务跑道（来自 TODO-live-trading §1）
- [ ] 模拟盘**连续、不被重置**运行满 **≥60 个交易日**（`portfolio_snapshots` 表行数）
- [ ] 净值达到通过线：初始 ×(1 + 目标年化 × 天数/365 × 0.7)
- [ ] 最大回撤未跌破 -20%
- [ ] 风控链路**真的触发过**：至少各一次 `risk_reject`、`stop_loss`、`strategy_pick`（查 `agent_decisions`）

> ⚠️ 用 `data/paper.db` 自查：
> ```sql
> SELECT COUNT(*) FROM portfolio_snapshots;                 -- ≥60 才够
> SELECT kind, COUNT(*) FROM agent_decisions GROUP BY kind; -- 看有没有 risk_reject / stop_loss / strategy_pick
> ```
> 若账本近期被 `reset_portfolio` 清空过，则 60 交易日从**最后一次重置**起算。

### 1.2 券商与权限（只有本人能办，见 TODO §2/§3）
> 已用账户 85530137 完成**只读**端到端验证（PR #126），故下列前三项已满足。
- [x] 已在券商开户（本项目实测券商：**江海证券**）
- [x] 已申请并开通「**QMT / 极速交易（量化）接口**」权限
- [ ] 满足资产门槛（多数券商 ≥50 万）——按实际账户资金核实
- [x] 拿到并安装了 QMT 客户端（含 `XtMiniQmt.exe` 交易端 + `miniquote.exe` 行情）

### 1.3 代码债（见第 7 节）
- [x] 第 7 节列出的 HIGH/MEDIUM 项**已全部修复**（PR #125–#134，§7.2 全 ✅）

---

## 2. 环境准备（Windows 机器，QMT 只有 Windows 版）

1. 一台**长开的 Windows 10/11**（物理小主机或云 ECS）。
2. 安装券商 QMT 客户端并能正常登录交易账户。
3. 准备 **bridge 专用 Python 环境**（3.10+，与 quanti 主环境隔离）：
   ```powershell
   python -m venv qmt-bridge-venv
   qmt-bridge-venv\Scripts\pip install xtquant   # 直连后端 XtDirectBackend(已真机验证)只需 xtquant
   # 注:vnpy / vnpy_xt 是【未验证 fallback】,对 miniQMT 有已知不兼容(见 xt_direct_backend.py 文件头),非必要不装
   ```
   > 本机已备好：`C:\Users\HuaWenbo\qmt-bridge-venv`（Python 3.13 + PyPI xtquant 250516，实测可连江海 miniQMT）。
4. 部署 quanti 主体（正常 `.venv`），确认 `quanti up` 能起 Web。

> **关键认知**：`xtquant` 与 quanti 主环境**互不干扰**——bridge 跑在含 xtquant 的环境里，quanti 只通过 HTTP（默认 `127.0.0.1:18099`）跟 bridge 通信。bridge 无需和 quanti 同一个 Python。

---

## 3. 启用步骤（严格分阶段，从只读 → 冒烟 → 小额 → 全量）

### 阶段 A：登录 QMT 交易端
1. 启动并登录 `XtMiniQmt.exe`，用**资金账号 + 交易密码**登录（**不要点取消**，否则会连行情一起关掉）。
2. 确认账户已登录：交易端能看到你的资金/持仓。
   > 校验：`userdata\users\<用户名>\Config.xml` 的 `<Accounts>` **非空**；若为空 / 日志报 `accountInfos not found`，说明没登录进交易端。

### 阶段 B：起 bridge（**下单默认关闭**），验只读链路
```powershell
$env:QMT_BRIDGE_BACKEND = "direct"
$env:QMT_ACCOUNT        = "<资金账号>"
$env:QMT_USERDATA_MINI  = "<...>\江海证券QMT实盘_交易\userdata_mini"
# 注意：此处【不设】QMT_BRIDGE_ALLOW_ORDERS —— 下单闸保持关闭
C:\Users\HuaWenbo\qmt-bridge-venv\Scripts\python.exe bridge\qmt_bridge.py --host 127.0.0.1 --port 18099
```
验证（另开一个终端）：
```powershell
curl http://127.0.0.1:18099/health
# 期望：{"mode":"xt","trader_connected":true,"datafeed_ok":true, ...}
curl http://127.0.0.1:18099/trader/asset          # 应返回真实资金
curl "http://127.0.0.1:18099/trader/positions"    # 应返回真实持仓
```
- `mode` 必须是 `xt`（不是 `mock`）。是 `mock` → 说明 xtquant 没装好或环境变量没设对。
- `trader_connected` 为 false → 回到阶段 A，账户没登录进交易端。

### 阶段 C：quanti 侧连上（仍只读观察）
```powershell
$env:QUANTI_ACCOUNT   = "live"
$env:QUANTI_LIVE_ACK  = "I_KNOW_REAL_MONEY"
quanti up --no-agent   # 实盘默认不自动拉起 Agent；--no-agent 更明确
```
- Web 打开，「当前持仓」应显示**真实账户**的持仓/资金。
- 此阶段 quanti 只读账户、不下单（下单闸在 bridge 侧仍关闭）。观察 1~2 天确认数据无误。
- Web「实盘控制」卡此时会出现（仅实盘账户可见）：红框危险区，显示实盘徽标、券商桥接健康（只读），下单开关应为**已撤防·观察模式**（默认）。观察期就保持撤防——即便有人误开了 bridge 侧下单闸，quanti 也会因未布防而拒掉每一笔买入。

### 阶段 D：开下单闸（**两道**）+ 手动冒烟一笔
> 真钱买入现在需要**两道下单闸同时打开**：bridge 侧 `QMT_BRIDGE_ALLOW_ORDERS=1`（部署闸）**和** quanti UI 的**布防**（运行时闸）。任缺一道，买入都被拒；卖出/止损/清仓不受任一道影响。

1. **停 bridge**，加上部署闸重启：
   ```powershell
   $env:QMT_BRIDGE_ALLOW_ORDERS = "1"   # ← bridge 侧部署闸:现在才允许真实下单
   C:\Users\HuaWenbo\qmt-bridge-venv\Scripts\python.exe bridge\qmt_bridge.py --port 18099
   ```
   重启后「实盘控制」卡的**部署闸**应显示**已放行**。
2. 在 Web「实盘控制」卡点【**布防实盘下单**】（会二次确认）→ 开关变为**已布防·放行真钱买入**。这是运行时闸，随时可一键撤防回观察。
3. 在 Web「手动下单」买 **100 股最便宜的标的**，确认：下单 → 成交 → 能撤单，全流程无误，且券商 App 里能看到这笔。
4. 卖掉这笔（T+1，次日）。确认卖出与对账正常。
5. 冒烟完若暂不继续，**先撤防**（一键回观察），再按需停 bridge。

### 阶段 E：小额跑 Agent
1. 先只放**约 5 万**资金到该账户。
2. **建议开观察期敞口硬闸**（quanti 侧，均只拦买入、永不拦止损/清仓，信任后调大或去掉）：
   - `QUANTI_MAX_ORDER_NOTIONAL`（元）：单笔买入名义额超限即拒 + 告警 `order_notional_capped`，防一笔铺满仓。例如 `=10000`。
   - `QUANTI_MAX_LIVE_EXPOSURE`（元）：现持仓市值 + 本单超此上限即拒 + 告警 `order_exposure_capped`，封顶整个账户敞口。例如观察期 `=50000`。
3. Web 上 `agent_start`，观察**一整周**：每天看 `/api/agent/decisions`、当前持仓、快照净值；确认止损/风控/换仓在真钱上按预期动作。

### 阶段 F：逐步加码
- 观察满意后，再把目标资金加上去；每加一档观察一周。

---

## 4. 日常运维 & 监控

- **每日**：看 `/health`（`mode=xt && trader_connected && datafeed_ok` 三者恒为 true）；看当日 `agent_decisions`、成交、快照净值与回撤。
- **bridge 健康**：`datafeed_ok` 由每 10s 心跳保活；若持续为 false → 交易端掉线或行情断流，quanti 会自动拒绝下单（安全），需人工查 QMT 客户端。
- **对账**：真实持仓/资金以**券商为准**；本地 DB 是镜像 + 审计记录。定期核对 Web 持仓 vs 券商 App。

---

## 5. 紧急回滚（任何时候出问题）

> 按**从软到硬**升级。关键区别：**撤防只停买入、保留卖出/止损/清仓**；而去掉 bridge 下单闸或关交易端会**连止损单一起挡掉**——手上有持仓、还指望止损保护时，别一上来就用最硬的那招。

0. **最快·首选**：Web「实盘控制」→【**撤防下单**】。一键停掉所有新买入，止损/止盈/清仓/熔断继续保护持仓。买错了/买太多的第一反应就是它。
1. Web →「停止 Agent」按钮，或 `quanti agent stop`，或 MCP `agent_stop` → 停掉自动循环。
2. 把 bridge 的 `QMT_BRIDGE_ALLOW_ORDERS` 去掉重启 → 禁止任何新下单（⚠️ 含止损/清仓等 exit，仅在确定要完全冻结时用）。
3. 直接**关掉 QMT 交易端** → 券商账户回到纯手动，任何自动单都发不出去。
4. DB 里持仓/订单/成交/决策全程留痕，可回放排查。

> 记住这条铁律：**关 QMT 交易端 = 最硬的急停**（也最粗暴——它同时废掉了止损）。**只想拦买入、保住止损，用「撤防」。**

---

## 6. 故障排查速查

| 症状 | 最可能原因 | 处理 |
|---|---|---|
| `/health` `mode=mock` | xtquant 没装 / 环境变量没设 / 未 `QMT_BRIDGE_BACKEND=direct` | 检查 bridge-venv 有 xtquant、三个 env 都设了 |
| `trader_connected=false` | 交易账户没登录进 XtMiniQmt | 阶段 A 重新登录（别点取消） |
| `datafeed_ok=false`（持续） | 交易端掉线 / 行情断流 | 查 QMT 客户端；quanti 此时会自动拒单 |
| 下单返回 `bridge orders disabled` | 下单闸没开 | 观察期正常；确认要下单再设 `QMT_BRIDGE_ALLOW_ORDERS=1` |
| quanti `is_connected()` False | `/health` 任一门不满足，或缺 `QUANTI_LIVE_ACK` | 逐项对照阶段 B/C |
| `RuntimeError: 拒绝构建实盘 broker...二次确认` | 设了 `QUANTI_ACCOUNT=live` 但没 `QUANTI_LIVE_ACK=I_KNOW_REAL_MONEY` | 补上二次确认 env |
| `RuntimeError: ...主机时区必须为北京时间` | 机器不是北京时间（云机常见 UTC）| **把机器时区设为 Asia/Shanghai** 后重启（已有启动断言拦截，见 7.2；确已用他法让 now() 返回北京时可 `QUANTI_ALLOW_NON_CN_TZ=1` 跳过）|
| 买单被拒 `观察期单笔名义额上限...` | 触发了 `QUANTI_MAX_ORDER_NOTIONAL` 单笔硬闸 | 观察期安全网正常；确认无误后调大或去掉该 env |
| 买单被拒 `实盘下单未布防(观察模式)` / 决策日志 `order_disarmed` | UI 下单开关处于**撤防**（默认）| 确认要下单 → Web「实盘控制」点【布防实盘下单】。卖出不受影响 |
| 「实盘控制」布防按钮**灰掉点不动** | 进程未带 `QUANTI_LIVE_ACK` → `live_capable=false` | 补 `QUANTI_LIVE_ACK=I_KNOW_REAL_MONEY` 重启后端（无它即便布防也不会真下单）|

---

## 7. 已知风险与限制（**上真钱前必读**）

### 7.1 真机未验证项（需在 QMT 机上实测）
> **真机下单冒烟已计划**，待交易时段 + 操作者在场按阶段 D 执行——这是上真钱前唯一剩下的技术验证步骤。
- **真实下单/撤单/成交回报**从未在真机跑过（只读链路已端到端验证，下单闸默认关）。阶段 D 的手动冒烟就是补这一步。
- `order_stock` 的成交回报、部分成交（partial）对账、连续下单节奏，需小额实盘观察确认。

### 7.2 代码债（已全部清零 ✅）
> 状态随修复进度更新。

| 优先级 | 项 | 现状 | 风险 |
|---|---|---|---|
| HIGH | 服务器时区断言 | ✅ 已修 | make_broker(live) 启动断言主机 UTC+8，否则拒建（`QUANTI_ALLOW_NON_CN_TZ=1` 可跳过）|
| HIGH | 观察期敞口硬闸 — 单笔 + 总敞口 | ✅ 已修 | `QUANTI_MAX_ORDER_NOTIONAL`（单笔名义额）+ `QUANTI_MAX_LIVE_EXPOSURE`（总持仓市值上限）；均 0=关、仅拦 BUY、永不拦 exit |
| HIGH | 订单幂等（client-order-id + 去重）+ mirror-before-POST + 对账 | ✅ 已修 | 每单带 client-order-id，bridge 按它去重（写进券商 order remark，跨重启仍认）+ 先写镜像再 POST + POST 异常标 submitting（不盲目重发、不被误撤）+ 每 tick 对账把 submitting 行按券商 remark 拉平（正向匹配、无匹配则保留不误撤） |
| MEDIUM | F3：pending 排队成交持久化 strength | ✅ 已修 | orders 表加 strength 列，入队持久化、成交按原始 conviction 定仓（不再硬编码 1.0）|
| MEDIUM | G2：日内下单计数重启回种（从 /trader/trades） | ✅ 已修 | 实盘 QmtBroker 启动时从 /trader/trades 回种当日买入计数，重启不再清零 |
| MEDIUM | 跨进程下单锁 | ✅ 已修 | 实盘 agent 启动取 DB 心跳单例锁；已有实盘进程在跑则第二个拒启（心跳失效自动可被接管） |
| 控制 | UI 运行时下单闸（布防/撤防，PR #136） | ✅ 已加 | `_send_now` 的 BUY 分支顶部读 `live_control.orders_armed`；默认撤防、盘中可一键切换、只拦买入永不拦 exit。与 bridge `QMT_BRIDGE_ALLOW_ORDERS` 双闸并存 |
| — | 其余见 `2026-06-22-live-readiness-audit.md` 六节 HIGH 清单 | 部分已修 | — |

### 7.3 设计边界
- bridge 无鉴权，**只绑 `127.0.0.1`**，切勿 `--host 0.0.0.0` 暴露（否则同机任何进程都能下真单）。
- 下单走**限价**（quanti 计算的价，clamp 到当日涨跌停带）；不支持市价单。
- 组合回撤熔断依赖 `/trader/asset` 的 `total_asset`；逐票止损依赖实时行情，行情断流时对应持仓会被跳过并告警（不会用成本价假装 pnl=0）。

---

## 8. 一页速记（老手直接看这个）

```
前提：模拟盘≥60交易日达标（当前唯一硬缺口；代码债已清、券商QMT权限已只读验证）+ 真机下单冒烟通过
1. 登录 XtMiniQmt（资金账号+交易密码，别点取消）
2. bridge（不设 ALLOW_ORDERS）：
   QMT_BRIDGE_BACKEND=direct QMT_ACCOUNT=... QMT_USERDATA_MINI=...\userdata_mini
   → curl /health 应 mode=xt&&trader_connected&&datafeed_ok
3. quanti：QUANTI_ACCOUNT=live QUANTI_LIVE_ACK=I_KNOW_REAL_MONEY quanti up --no-agent  → 只读核对
4. 开双闸：①加 QMT_BRIDGE_ALLOW_ORDERS=1 重启 bridge ②Web「实盘控制」点【布防】→ Web 手动 100 股冒烟 → 撤/卖验证
5. 放 5 万 → agent_start 观察一周 → 满意再加码
急停（软→硬）：Web【撤防】(只停买入、留止损) → 停 Agent → 去 ALLOW_ORDERS 重启(连 exit 一起冻) → 关 XtMiniQmt(最硬)。
```
