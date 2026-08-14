# 部署与运维(稳定运行手册)

本文档覆盖进程守护、每日自动体检、数据备份与常见故障排查。目标是让
`quanti serve` 不再依赖手动 nohup + 人肉盯日志。

## 0. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `QUANTI_ACCOUNT` | paper | 账户库(paper/live) |
| `QUANTI_STACK_DUMP` | 0 | =1 时 `kill -USR1` 转储全线程栈(随后进程按信号默认动作退出,KeepAlive 拉起) |
| `QUANTI_HOOK_WARMUP_SEC` | 1800 | 启动后多久才允许重活钩子(doctor/策略闸门/因子重评)——避免与冷启动首轮 tick 的 selector sweep 争 CPU/锁 |
| `QUANTI_SQLITE_CACHE_MB` | 64 | 每个 SQLite 连接的页缓存上限(MB,最小 8)。OS 页缓存已覆盖热文件页,256MB 的 SQLite 缓存是纯重复占用——内存紧张时先看这里 |

## 0.1 系统要求与内存排查

- **内存**:16GB 起步(2.3GB 市场库 + 全市场扫描的 pandas 瞬态帧)。
- **swap 排查**(macOS):`sysctl vm.swapusage`——used 接近 total 时,系统整体
  变慢会放大一切量测(2026-08-15 实测 swap 10.4GB/11.3GB 时,同一 sweep 从
  88s 恶化到 14 分钟)。swap 压力来自宿主机的全部应用,不只是 quanti;
  释放内存、或把 `QUANTI_SQLITE_CACHE_MB` 降到 32,是两条最直接的缓解。

## 1. 进程守护(launchd, macOS)

`deploy/com.quanti.api.plist` 是 API 服务的 launchd 模板:崩溃自动重启
(KeepAlive)、开机自启(RunAtLoad)、日志落到 `logs/api.{out,err}.log`。

安装(把 REPLACE_ME 换成你的绝对路径,plist 里共 4 处):

```bash
# 1. 改好路径后:
mkdir -p logs
cp deploy/com.quanti.api.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.quanti.api.plist
# 2. 验证
curl -s http://127.0.0.1:8000/api/health   # {"status":"ok"}
```

日常操作:

```bash
launchctl list | grep quanti          # 看状态(第二列是 PID,非 0 退出码列有记录)
launchctl unload ~/Library/LaunchAgents/com.quanti.api.plist   # 停
launchctl load   ~/Library/LaunchAgents/com.quanti.api.plist   # 启
tail -f logs/api.err.log              # 看日志
```

升级流程(避免升级时服务自启抢旧进程):

```bash
launchctl unload ~/Library/LaunchAgents/com.quanti.api.plist
git -C /path/to/quanti pull --ff-only
# 前端有改动时:cd web && npm run build
launchctl load ~/Library/LaunchAgents/com.quanti.api.plist
```

> Linux 服务器用 systemd 等价物即可(单元文件核心只有 ExecStart=
> `python -m quanti.cli serve` + Restart=always,路径写法照搬 plist)。

## 2. 每日自动体检(doctor)

后台同步守护每天 17:45(当日行情落地、regime 快照之后)自动跑一次体检,
结果写进决策日志(decision log):Web「AI Agent」页可见 `doctor_ok` /
`doctor_warn` 条目。体检三项:

| 检查 | 内容 | 失败含义 |
|---|---|---|
| exit_coverage | 持仓的入场策略是否仍在 strategies 目录 | 某持仓只剩止损/止盈离场(策略被精简进 attic) |
| data_freshness | 每个代码的最新 bar 是否跟上交易日历 | 有代码缺数据/长期落后(可能停牌,需人工确认) |
| db_integrity | SQLite quick_check(main + market) | 库文件损坏,立即备份排查 |

手动跑:`quanti doctor`(人类可读,发现问题时退出码 1);
`quanti doctor --json --codes 000001,600519`(机器可读、只查指定代码)。

Web 侧:持仓策略离场降级会在「AI Agent」页顶部亮红色告警卡
(列出代码与缺失策略);无退化时不显示。

### 死锁诊断:QUANTI_STACK_DUMP

部署 plist 里设 `QUANTI_STACK_DUMP=1` 后,`kill -USR1 <pid>` 会把**全线程的
Python 栈**打到 err log。注意语义(macOS 实测):faulthandler 转储完成后按
信号的默认动作执行——对 SIGUSR1 即**进程退出**,KeepAlive 会在 10 秒内
拉起新进程。所以这是「转储 + 重启」组合:抓完现场服务自己复活,诊断用,
别在交易时段当玩具按。

## 3. 告警静默的已知问题(已修)

历史上挪走策略(如精简进 attic)后,盘中守卫每 5 秒刷一条
"entry_strategy ... not loaded" 警告——一天约 5 万行日志垃圾,且策略离场
静默失效。现在:

- 同一 (策略, 代码) 每天只告警一次(quanti/execution/exits.py);
- 退化持仓在 Agent 页红色卡片 + 每日 doctor 决策日志两处可见。
- **attic 回退**(2026-08-14):离场回放会回退扫描 strategies/attic——策略被
  精简进 attic 不再导致离场降级(开仓时用的就是这套逻辑,离场保持一致),
  选股器仍只看主目录,不会把退役策略重新纳为候选。

**处置**(仅当策略在 strategies/ 与 attic 同时消失):把策略文件放回任一目录,
或平掉该持仓,或显式接受仅止损/止盈离场。
不要放着不管——策略离场是开仓逻辑的配套,缺了它出场逻辑就少一半。

## 4. 数据备份

SQLite 用 WAL 模式,直接 cp 主文件可能丢未 checkpoint 的事务。备份用:

```bash
sqlite3 data/paper.db  "VACUUM INTO 'backups/paper-$(date +%F).db'"
sqlite3 data/market.db "VACUUM INTO 'backups/market-$(date +%F).db'"  # 2.3GB,视磁盘而定
```

实盘账户(live)建议每日备份 account db;market.db 每周即可。

## 5. 每周人工核查清单(接实盘前必备,见 docs/TODO-live-trading.md)

- [ ] `quanti doctor` 三绿
- [ ] Web 决策日志最近 3 天有 `doctor_ok` 且无 `doctor_warn`
- [ ] Agent 页无「持仓策略离场降级」红卡
- [ ] 组合净值曲线无异常跳变(对账)

