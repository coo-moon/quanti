# 交易核心审计报告 — 正确性 & 实盘安全

**日期**: 2026-06-20  
**范围**: 策略 / 回测 / 选股 / 模拟盘 / 风控 / 信号融合 / agent 周期  
**方法**: 8 模块并行审计 + 每条发现独立对抗式证伪(58 agents)  
**结果**: 33 条确认为真 / 17 条证伪过滤  
**严重度**: 23 HIGH / 10 MEDIUM / 0 LOW

> 结论：上实盘前必须先修 HIGH 项。绝大多数为既有核心代码问题(回测/风控/选股引擎)，非 QMT 脚手架。优先级：①回测正确性/回测≡实盘 → ②风控窟窿 → ③日内上限重置 → ④选股指标。

---

## 风控未真正生效 (risk-not-enforced) — 13 条

### [HIGH] Per-stock 10% and total 80% caps are checked on PRE-trade ratios, so a single buy can blow far past them
- **位置**: `quanti/risk/manager.py:47-60; enforced from engine.py:163-167 and _execute_buy:240-247`  
- **模块**: backtest-engine

**问题**: RiskManager.check evaluates the EXISTING position ratio: total check uses portfolio.market_value/total_value BEFORE the new buy (manager.py line 50-52), and single-stock uses pos.market_value only `if signal.stock_code in portfolio.positions` (line 55-60) — a brand-new position has no entry yet, so the single-stock 10% cap is NOT applied to first entries at all. The engine's buy sizing (engine.py _execute_buy) caps spend at 95% of cash (line 240) and 10000 shares (line 247) but applies NO per-position notional cap. So with an empty portfolio, the first BUY can deploy up to 95% of equity into ONE stock — the 10% single-stock limit is silently bypassed. Even for existing positions, because the check is pre-trade, a position at 9.9% passes and the add-on can push it well above 10%. Note PaperBroker DOES size to max_position_pct (paper_broker.py line 653 size_cap), so the BACKTEST enforces weaker caps than the live broker — another backtest/live divergence on top of the risk hole.

**实盘影响**: Backtests can show a strategy concentrating ~95% into one name and 'working', then the live broker (which DOES cap at 10%) trades a completely different, diversified book — so the backtested equity curve is not what live produces. Separately, the documented 'max 10% per stock / max 80% total' safety promise is not actually enforced in the backtest, so a strategy validated there has unbounded concentration risk that the user believes is capped.

**建议修法**: Enforce caps on POST-trade state: compute the resulting single-stock weight and total invested weight after the proposed quantity and reject/shrink to fit max_position_pct and max_total_position_pct. Apply the same sizing in the backtest engine as in PaperBroker (notional size_cap = total_value*max_position_pct) so backtest and live agree, and so first entries are also capped.

### [HIGH] Daily-trade-limit counter never resets — broker permanently blocks all buys after 20 lifetime trades
- **位置**: `quanti/execution/paper_broker.py:542, 592, 731, 780 (record_trade) vs risk/manager.py:63,128-134`  
- **模块**: paper-broker

**问题**: PaperBroker constructs its own RiskManager (paper_broker.py:82) and calls self._risk.record_trade() on every fill (lines 542, 592, 731, 780), which increments RiskManager._daily_trade_count. RiskManager.check() rejects buys once _daily_trade_count >= max_daily_trades (default 20) (manager.py:63). The ONLY reset is RiskManager.reset_daily() (manager.py:128), and a grep of the whole codebase shows the agent runtime never calls reset_daily on the broker's risk manager — only the BacktestEngine does (engine.py:145). The PaperBroker is documented as a single long-lived instance per process (paper_broker.py:45). Therefore _daily_trade_count grows monotonically for the life of the process and is never zeroed.

**实盘影响**: After the 20th cumulative fill (not 20 per day — 20 ever, across the whole process lifetime), RiskManager.check() returns False for every subsequent BUY with reason 'Daily trade limit (20) reached'. In pending mode this rejects at queue time AND at fill time, so the system silently stops opening any new positions and the operator sees only generic risk_reject logs. The intended risk control (cap trades PER DAY) is not what executes; it is an absolute lifetime cap. This also diverges from backtest, which resets the counter daily, so a strategy that passed backtest will behave completely differently live.

**建议修法**: Call self._risk.reset_daily() once per trading day in the live path — e.g. at the start of try_fill_pending_orders()/the agent tick when the date rolls over (track last-reset date), mirroring BacktestEngine.run() which calls reset_daily() each day. Add a test that submits >20 trades across two simulated days and asserts buys on day 2 are still accepted.

### [HIGH] max_industry_pct (30% per-industry cap) is declared but never enforced anywhere
- **位置**: `quanti/risk/manager.py:manager.py:15 (declared); no reader in RiskManager.check or PaperBroker`  
- **模块**: paper-broker

**问题**: RiskConfig.max_industry_pct = 0.30 is defined (manager.py:15) and described as 'Max 30% per industry', but a full-repo grep shows the only occurrence is the declaration — RiskManager.check() (manager.py:41-66) checks total ratio and single-stock ratio but never reads max_industry_pct, and PaperBroker never computes any industry exposure. The stock industry field exists in the DB (database.py StockInfo.industry) but is not consulted on the buy path.

**实盘影响**: The system advertises an industry-concentration limit it does not apply. Live, the portfolio can become heavily concentrated in a single sector (e.g. all picks happen to be the same hot industry) with no guard, exactly the tail-risk the limit is supposed to prevent. A correlated sector drawdown can blow well past the risk the operator believes is capped.

**建议修法**: Either implement the industry check in RiskManager.check() (sum market_value of held positions in the candidate's industry + the prospective buy, reject if > max_industry_pct * total_value; PaperBroker would need to pass industry lookups in), or, if it is intentionally unimplemented, remove the field and its docstring so it does not read as an enforced limit.

### [HIGH] portfolio_stop_loss_pct (-15% portfolio drawdown stop) is declared but never enforced
- **位置**: `quanti/risk/manager.py:manager.py:18 (declared); no reader anywhere`  
- **模块**: paper-broker

**问题**: RiskConfig.portfolio_stop_loss_pct = -0.15 is defined and documented as a '-15% portfolio drawdown stop' (manager.py:18). A full-repo grep shows it has no reader: RiskManager.check_exits()/check_stop_loss() (manager.py:68-126) only apply the PER-STOCK stop_loss_pct and the trailing take-profit; there is no portfolio-level equity-drawdown check in PaperBroker.check_exits() (paper_broker.py:791) or anywhere else.

**实盘影响**: There is no circuit-breaker that flattens or halts trading when the whole account draws down past 15%. The operator believes a hard portfolio stop exists; live, the account can keep trading straight through a 15%/25%/40% drawdown with only the per-name 8% stops firing piecemeal. This is the single limit most likely to be relied on for capital preservation, and it does nothing.

**建议修法**: Implement a portfolio drawdown check that compares current total_value against the peak total_value (from portfolio_snapshots / a tracked high-water mark) and, when the drawdown breaches portfolio_stop_loss_pct, triggers flatten()/cancel_all_pending() and blocks new buys. Wire it into check_exits() or the agent tick. Add a test driving total_value down 16% and asserting the stop fires.

### [HIGH] Industry concentration limit (max_industry_pct 30%) is never enforced — advertised hard limit is a no-op
- **位置**: `quanti/risk/manager.py:15 (config), 41-66 (check); models.py:92-115 (Position has no industry)`  
- **模块**: risk-manager

**问题**: RiskConfig.max_industry_pct=0.30 is documented (manager.py:15, USAGE.md:1083 '单行业不超过 30%') as a hard risk floor, but RiskManager.check() (lines 41-66) contains NO industry logic whatsoever. The Portfolio/Position models (models.py:117-130, 92-115) carry no industry field, so the risk layer literally cannot compute per-industry exposure. The only industry handling in the whole system is signal_pipeline.industry_cap (signal_pipeline.py:192) which (a) caps the COUNT of candidates per industry (max N names), not the 30% capital weight, (b) is gated behind an opt-in goal.params['industry_neutral'] flag that defaults to False (runtime.py:392), and (c) runs in the candidate-selection path, not the risk floor. Both PaperBroker._submit/_fill and QmtBroker._submit_signal call only self._risk.check(), which never looks at industry. Result: the system can put 100% of capital into one industry and no risk check objects.

**实盘影响**: With real money, a momentum/factor run can pile the entire 80% invested budget into a single hot sector (e.g. all semis or all liquor) with zero risk-layer resistance. The single biggest concentration risk the config claims to bound is completely unbounded. A sector crash takes the whole book down — exactly the scenario the 30% cap was meant to prevent.

**建议修法**: Either (a) implement the check in RiskManager.check(): look up signal.stock_code's industry (via db.get_stock), sum post-trade market value of held positions in that industry plus the intended order notional, reject if it would exceed max_industry_pct * total_value; this requires passing industry info into the risk layer (e.g. an industry map or enriching Portfolio/Position with industry). Or (b) if industry capping is intentionally delegated to industry_cap, make it a non-optional part of the floor and document that max_industry_pct in RiskConfig is unused/dead. Do NOT ship to live with this field advertised but inert.

### [HIGH] Single-stock cap (max_position_pct 10%) is not enforced for NEW positions and is not post-trade in check()
- **位置**: `quanti/risk/manager.py:54-60`  
- **模块**: risk-manager

**问题**: check() only evaluates the per-stock ratio inside `if signal.stock_code in portfolio.positions:` (line 55). For a brand-new buy of a name not yet held, the per-stock block is skipped entirely and the signal passes regardless of order size. Even for an existing holding, the ratio uses the CURRENT market value (pos.market_value / total_value) — a pre-trade snapshot that ignores the incoming buy quantity — so an add-on buy that pushes a 9.9% position to 25% passes check() because the *pre-trade* ratio was still 9.9% (<10%). The actual 10% enforcement lives only in the brokers' sizing math (paper_broker.py:653 size_cap, qmt_broker.py:248 stock_room), which IS post-trade-aware for the buy notional. So the RiskManager 'hard floor' for single-stock is effectively a no-op; the real cap is a sizing convenience in each broker that any new/alternate execution path could omit.

**实盘影响**: The risk layer that's supposed to be the independent backstop does not actually backstop single-stock concentration. If a future code path (or the live QmtBroker, whose _size_buy is the only enforcer) has a sizing bug, nothing in check() catches an oversized single-name fill. Backtest vs live also diverge subtly: the backtest engine relies on check() for the gate (engine.py:163-164) and on its own _execute_buy cash cap, so a strategy validated in backtest may take larger single-stock weights than the 10% the owner believes is enforced.

**建议修法**: Make check() compute the POST-trade single-stock ratio for the incoming BUY: (existing_mv + intended_order_notional) / (total_value) <= max_position_pct, for BOTH new and existing names. This requires the order's intended notional be available at check time (pass it in, or have check() reject only the clearly-over case and keep sizing as the fine cap). At minimum, drop the `if code in positions` guard so a new oversized name can be rejected, and base the ratio on post-trade value.

### [HIGH] reset_daily() never called in the live/paper runtime — daily-trade cap permanently locks out buys after 20 cumulative trades
- **位置**: `quanti/risk/manager.py:128-134 (reset_daily/record_trade); only caller is backtest/engine.py:145`  
- **模块**: risk-manager

**问题**: RiskManager._daily_trade_count is incremented by record_trade() on every fill (paper_broker.py:542/592/731/780, qmt_broker.py:217) and gated in check() at line 63 (`>= max_daily_trades`). But reset_daily() is ONLY called in backtest/engine.py:145 (once per simulated day). A grep across the repo shows NO call to reset_daily() in runtime.py, PaperBroker, or QmtBroker. The RiskManager instance lives for the entire process lifetime (created once in the broker __init__). So in a long-running live/paper agent process, _daily_trade_count is a monotonically increasing lifetime counter that is never reset at the start of a trading day. qmt_broker.py:215-216 explicitly acknowledges 'reset_daily() at session start ... is phase-③' — i.e. not done.

**实盘影响**: Two compounding live hazards: (1) The cap is not 'daily' at all — it's a lifetime-of-process cap. After 20 total fills (could span many days), check() rejects ALL further buys silently (sells still pass). A live agent that has been up for a week suddenly stops buying with reason 'Daily trade limit reached', looking like a bug, while the operator thinks the limit resets each morning. (2) Conversely the protection the cap is meant to give (cap intraday churn) is absent within any single day if the process restarts. Either way the limit does not behave as documented.

**建议修法**: Call self._risk.reset_daily() at the start of each trading day in the live tick (e.g. in runtime._run_one_cycle when the date rolls over, tracking last-seen trade date), and for QmtBroker seed _daily_trade_count from the venue's count of today's trades (/trader/trades) on session start so a process restart mid-day doesn't reset the real count to 0. Until then, document that the cap is process-lifetime, not daily.

### [HIGH] Portfolio-level drawdown stop (portfolio_stop_loss_pct -15%) is never enforced
- **位置**: `quanti/risk/manager.py:18 (config); no reader anywhere`  
- **模块**: risk-manager

**问题**: RiskConfig.portfolio_stop_loss_pct=-0.15 is documented as a '-15% portfolio drawdown stop' (manager.py:18, USAGE.md:1086 '组合止损 -15%'). A grep for portfolio_stop_loss across quanti/ returns only the config definition — there is no code that reads it. check_exits() (lines 83-126) and check_stop_loss() (68-81) only implement PER-POSITION stop-loss (pos.pnl_pct <= stop_loss_pct), trailing take-profit, and strategy exit. Nothing computes total portfolio drawdown vs initial/peak equity and flattens or halts trading when the book is down 15%.

**实盘影响**: The system's last-resort capital-preservation guard does not exist. A market-wide drawdown where individual names each stay above the -8% single-stop (e.g. a slow -12% grind across many positions) never triggers any portfolio-level protection, and the book can blow well past the -15% the owner believes is a hard floor. This is the kind of limit whose absence is only discovered after a large live loss.

**建议修法**: Implement a portfolio drawdown guard: track peak equity (or use initial_cash), compute current total_value drawdown, and when it breaches portfolio_stop_loss_pct, trigger flatten() and/or halt new buys for the session. Wire it into the runtime tick (and document whether it measures from peak or from initial). If it's intentionally not yet implemented, remove the field/doc so it isn't mistaken for an active limit.

### [HIGH] Runtime never checks broker.is_connected() before trading or running stop-loss with the live venue
- **位置**: `quanti/agent/runtime.py:590-743 (whole _run_one_cycle); is_connected defined base.py:88, qmt_broker.py:67, but grep finds zero callers in quanti/agent`  
- **模块**: agent-cycle

**问题**: base.Broker defines is_connected() specifically so 'the runtime should refuse to trade rather than silently queue orders that will never reach the venue' (base.py:90-96), and QmtBroker.is_connected() does a real /health probe. But grep for is_connected across quanti/agent returns NO matches — _run_one_cycle calls try_fill_pending_orders, check_exits, and execute_signals unconditionally. When the qmt-bridge / QMT client is down, QmtBroker._submit_signal's call to self._client.post raises or returns ok=False, every order is mirrored as 'rejected', check_exits() catches the bridge exception in _reconciled_portfolio and effectively no-ops, and the tick logs a normal 'cycle' summary with 0 fills. The agent believes it is operating normally.

**实盘影响**: If the bridge dies while positions are open, stop-loss and trailing-take-profit exits silently fail every tick (check_exits gets an empty/erroring reconciled portfolio), so a -8% stop never fires and losses run unbounded until someone notices. New buys are dropped as 'rejected' with no alarm. The system gives no signal that it is flying blind on live money.

**建议修法**: At the top of _run_one_cycle, if not self._broker.is_connected(): log a loud 'broker_disconnected' decision, set a degraded status flag, and return without attempting fills/exits/buys. Optionally trigger a push/alert. Re-probe each tick and resume only when healthy.

### [MEDIUM] max_total_position_pct (80%) only blocks when ALREADY at limit, never enforces 80% as a ceiling on the new trade
- **位置**: `quanti/risk/manager.py:49-52`  
- **模块**: backtest-engine

**问题**: The total-position gate rejects a BUY only if current position_ratio >= 80% (line 51). If the portfolio is at 79% invested, the check passes and the engine's _execute_buy then deploys up to 95% of REMAINING cash (engine.py line 240), pushing total invested above 80%. The limit is a tripwire on the prior state, not a constraint that bounds the resulting allocation to <=80%.

**实盘影响**: The '最多 80% 仓位' guarantee (documented in docs/USAGE.md) is breachable by one trade. Backtests can run fully invested past the intended 80%, overstating returns in up-markets and understating cash-buffer drag, so the strategy's risk profile shown to the user is wrong.

**建议修法**: Bound the buy by the remaining headroom to the 80% ceiling: max additional notional = max(0, 0.80*total_value - current_market_value), and size the order to not exceed it. Combine with the per-stock cap fix above.

### [MEDIUM] Daily-trade limit (max_daily_trades) is not enforced consistently — risk-driven exits bypass the counter, and counter is only bumped in engine, not at check time
- **位置**: `quanti/backtest/engine.py:144-153, 173-174`  
- **模块**: backtest-engine

**问题**: In run(), risk-driven exits (check_exits, lines 146-153) call _process_signal directly and never call self._risk.record_trade(), so stop-loss/take-profit fills do not increment _daily_trade_count. Only strategy-generated fills bump it (line 173-174). RiskManager.check also early-returns True for all SELLs (manager.py line 43-44), so SELLs are never gated by the daily limit anyway. Net: the max_daily_trades=20 cap counts only strategy BUYs/SELLs that pass through on_bar, undercounting actual executions. A flapping strategy plus stop-losses can execute many more than 20 trades/day in backtest.

**实盘影响**: The daily trade cap (a churn/cost control) is looser in backtest than the stated limit, so backtested transaction costs are understated relative to a live system that actually enforces it — and if live enforces it strictly, live will skip trades the backtest took, diverging again.

**建议修法**: Increment the trade counter on EVERY fill (including risk exits) and decide whether SELLs/exits should count against the cap; apply the same rule in backtest and in PaperBroker so the cap means the same thing in both.

### [MEDIUM] Add-on buys reset T+1 tracking to the original buy_date — newly bought shares become sellable too early
- **位置**: `quanti/execution/paper_broker.py:paper_broker.py:526-528 & 711-712 (upsert with existing['buy_date'] or bar_date)`  
- **模块**: paper-broker

**问题**: On a follow-on buy into an existing position, both fill paths upsert with buy_date = existing['buy_date'] or bar_date (paper_broker.py:528 pending, 712 immediate) — i.e. they KEEP the original (older) buy_date and discard today's. The whole-position SELL T+1 guard then compares pos['buy_date'] == fill bar_date (paper_broker.py:564, 750). Because buy_date stays at the original (earlier) date, shares added TODAY are treated as sellable on the next bar, and a SELL liquidates pos['quantity'] (the ENTIRE position including today's lot) (paper_broker.py:570). The system tracks only one buy_date per code and sells all-or-nothing, so per-lot T+1 is not modelled.

**实盘影响**: Live (and in QMT) the shares bought today are NOT sellable today; a sell order for the full quantity will be partially rejected by the venue for the unsettled lot. The paper broker books a full-quantity sell as filled, so paper/backtest P&L and position state diverge from what the live venue actually does on any name that was averaged into the same day it is exited. Cash and position reconciliation against the real account will drift.

**建议修法**: Track sellable vs frozen quantity per position (e.g. store today's bought quantity / a settlement date) and cap the SELL at the sellable amount, matching how QMT enforces T+1. On an add-on, do not silently keep the old buy_date — record the unsettled lot. Add a test: buy day D, add-on day D+1, sell day D+1 should only sell the D lot.

### [MEDIUM] Total-position cap (80%) uses pre-trade ratio and ignores incoming order size; boundary lets a buy push past 80%
- **位置**: `quanti/risk/manager.py:47-52`  
- **模块**: risk-manager

**问题**: The total-position check computes position_ratio = portfolio.market_value / total_value using PRE-trade values and rejects only when already `>= max_total_position_pct` (line 51). It does not account for the size of the incoming buy. At, say, 79% invested the check passes, and the subsequent fill can deploy up to cash*0.95 (paper_broker.py:668 / qmt_broker.py:249), pushing invested well above 80% in a single order. The brokers' single-stock size_cap does not bound the aggregate, so total exposure can settle above the advertised 80% ceiling after one buy. Also, because it only blocks at >= the limit, the limit behaves as 'stop buying once already over' rather than 'never let a buy take us over'.

**实盘影响**: The 80% invested ceiling (meant to keep a cash buffer for T+1 settlement and exits) can be breached, leaving less cash than intended and overexposing the book right before a drawdown. Divergence from the documented limit means live exposure exceeds what the owner validated.

**建议修法**: Make the total-position check post-trade and order-aware: reject (or have the sizer cap) the buy so (market_value + intended_notional) / total_value <= max_total_position_pct. At minimum cap the buy notional to the remaining room (total_value*0.80 - market_value) rather than only blocking when already over.

---

## 前视偏差 (look-ahead) — 4 条

### [HIGH] Look-ahead: strategy decides on today's close AND the backtest fills at that same close
- **位置**: `quanti/backtest/engine.py:156-170, 238, 253, 320`  
- **模块**: backtest-engine

**问题**: In run(), for each current_date the engine calls strategy.on_bar(bar) (line 157) and then _process_signal -> _execute_buy/_execute_sell, which fill at bar.close: buy price = bar.close*(1+slip) (line 238/253), sell price = bar.close*(1-slip) (line 320). The strategies decide ON that same close: turtle_breakout.py line 38 `if bar.close > entry_high`, ma_cross.py line 27-34 computes MAs that include the current bar's close. So the engine uses information (today's settle price / today's full-bar indicators) that is only known AFTER the close to transact AT the close. This is the classic same-bar look-ahead. It is physically impossible live: you cannot observe the closing print and simultaneously get filled at it. The risk-exit path has the same flaw — current_price is set to today's close (line 132) and check_exits compares pos.pnl_pct (line 105 of risk/manager.py) then fills at today's close (line 320), so stop-losses also 'see' the close they trade at.

**实盘影响**: Every backtest is systematically optimistic. Live, the soonest realistic fill is the next bar's open (which is what PaperBroker's pending mode actually does — open of next trading bar, paper_broker.py line 345). A breakout/MA strategy that buys exactly at the breakout close in backtest will, live, pay the gap-up next-open; a stop-loss that exits at the trigger close will, live, exit at next-open (often a worse gap-down). Backtest Sharpe/return will not be reproduced with real money.

**建议修法**: Fill at the NEXT bar's open, not the current bar's close. Either (a) defer execution: on day t, on_bar produces signals; execute them against day t+1's open; or (b) feed on_bar the bar up to t-1 and fill at t's open. This also makes the backtest agree with PaperBroker pending mode (fill_basis='open', next_trading_bar). Until fixed, treat all backtest metrics as upper bounds.

### [HIGH] Backtest fills strategy signals at the SAME bar's close that generated them (look-ahead)
- **位置**: `quanti/backtest/engine.py:156-170, 238, 253, 320`  
- **模块**: strategies

**问题**: In BacktestEngine.run, for each current_date the loop fetches today_bars and calls strategy.on_bar(bar) (line 157). Every shipped strategy makes its decision using the CURRENT bar's close: e.g. ma_cross.py:28-30 compute short_ma_curr/long_ma_curr including prices[-1] (today's close); macd_cross.py:36-37 read dif.iloc[-1]; bollinger_band.py:41 close_curr = prices[-1]; turtle_breakout.py:38 `if bar.close > entry_high`; rsi_ob_os.py:42 rsi_curr; ma_volume.py:34-40. The resulting signal is then executed in the SAME iteration via _process_signal, and _execute_buy fills at `price = bar.close * (1 + slip)` (engine.py:238/253) and _execute_sell at `price = bar.close * (1 - slip)` (engine.py:320). So the engine observes today's close to make a decision AND fills at today's close. In live A-share trading you cannot see the daily close and still transact at that close — the order goes into the next session. This is classic same-bar look-ahead and makes every backtest used by the StrategySelector optimistic.

**实盘影响**: Backtests (and the walk-forward OOS metrics that rank strategies for live deployment) are systematically optimistic because they buy/sell at a price that was only knowable after the decision could no longer be acted on. Live fills happen at the NEXT bar's open (paper_broker pending mode, line 345), which can gap away from the close that triggered the signal. Strategies that look great in selection can lose money live, and the WRONG strategy can be promoted to real money.

**建议修法**: Execute signals generated on bar t at bar t+1's OPEN, exactly as the live PaperBroker pending path does (fill_basis='open'). Concretely: collect signals on day t, then on day t+1 fill them at that day's open (with slippage), enforcing T+1. This aligns the backtest with the live fill model and removes the same-bar peek. At minimum, document and quantify the bias; ideally make the backtest engine and PaperBroker share one fill function.

### [HIGH] Live signal/factor generation runs intraday on a partial (non-final) daily bar — look-ahead vs. its own backtest
- **位置**: `runtime.py:357-373 (_single_strategy_signals), 410-415 (_compute_fused_candidates), 280-308 (_ensure_recent_data); quanti/agent/signal_pipeline.py:238-240; quanti/factors/cross_sectional.py:190`  
- **模块**: signal-pipeline-factors

**问题**: The agent ticks every 4h (runtime.py:74 tick_interval_sec=4h) with NO trading-session/'bar is final' guard in _run_one_cycle (runtime.py:582). On every tick _ensure_recent_data() syncs AkShare with end=date.today() (runtime.py:280,307), which during market hours returns TODAY's still-forming bar whose `close` is the current intraday price, stored as the daily close. collect_signals_per_strategy / _single_strategy_signals then replay on_bar up to and INCLUDING that bar (start/end both default to date.today(), runtime.py:357-359; signal_pipeline.py:238) and emit a BUY off today's partial close. compute_factor_panel likewise defaults as_of=date.today() (cross_sectional.py:190); factor_reversal_1w uses end_offset=0 (cross_sectional.py:81) so it consumes today's intraday price directly. The Selector/walk-forward backtest that VALIDATES these strategies fills BUYs at the same bar's close (backtest/engine.py:253 `price = bar.close*(1+slip)`). So the backtest assumes 'decide on the final close, fill at that close' — physically impossible — and live decides on an intraday snapshot masquerading as a close.

**实盘影响**: Strategies validated by a 'decide-and-fill-at-close' backtest behave differently with real money: the close that triggered the BUY in backtest is unknowable at decision time live; the live fill happens against a moving intraday price, and end-of-day the bar can revert, so realized entries and signal triggers diverge from every backtested metric. Optimistic backtests, real losses.

**建议修法**: Gate _run_one_cycle so signal/factor generation only uses COMPLETED sessions: compute signals as_of the last fully-closed trading day (e.g. yesterday's close, or today only after market close), and pass that explicit as_of into both compute_factor_panel and collect_signals_per_strategy. Make the backtest fill at NEXT bar's open (T+1 open) to match a realistic 'decide on close N, execute on open N+1' policy, and align live execution to the same. Add an is-final-bar / trading-session check before generating signals.

### [MEDIUM] Intraday tick can decide on today's not-yet-closed bar (partial-close look-ahead), trading live off an incomplete price
- **位置**: `quanti/agent/runtime.py:348-373 (_single_strategy_signals: end=date.today(), acts on bars >= today-3d) + _ensure_recent_data 268-310 syncs through date.today(); signal_pipeline.py:238-259`  
- **模块**: agent-cycle

**问题**: _single_strategy_signals and collect_signals_per_strategy fetch bars up to end=date.today() and treat any bar with date >= today-3 days as actionable, calling strategy.on_bar() which (e.g. MACrossStrategy.on_bar, ma_cross.py:21-54) computes its MA cross off bar.close and emits a BUY/SELL for that bar. _ensure_recent_data syncs from AkShare through date.today() each tick (runtime.py:281,307). The default loop runs immediately on start and every 4h (runtime.py:74,172-177), so a tick can land during the 09:30-15:00 session. If AkShare returns an in-progress daily row for today, the strategy makes its cross decision using an INTRADAY 'close' that is not final. In PaperBroker pending mode the fill is next-day open so the price path is at least forward, but the SIGNAL itself was formed on a non-final close that may not exist by 15:00. On the QmtBroker live path the signal is acted on immediately at the live quote — deciding and trading on the same incomplete bar.

**实盘影响**: Signals generated mid-session can fire on a close that reverses by the actual market close, producing entries/exits the strategy would never have taken on the real daily bar — and live (QmtBroker) acts on them the same day. Backtests, which only ever see completed daily bars, never reproduce this, so the behavior is unvalidated.

**建议修法**: Gate signal generation to completed bars only: when running during/after a session, drop any bar whose date == today unless the session has closed (use is_market_open / a settled-bar check), or only act on bars strictly before today. Make _ensure_recent_data not treat an intraday partial row as the actionable bar.

---

## 回测≠实盘 (backtest-vs-live) — 9 条

### [HIGH] Backtest fills at CLOSE while live (PaperBroker pending) fills at next-open — validated strategies trade differently live
- **位置**: `quanti/backtest/engine.py:238/253/320 vs C:/Users/HuaWenbo/Documents/GitHub/quanti/quanti/execution/paper_broker.py:345-347`  
- **模块**: backtest-engine

**问题**: The backtest engine fills synchronously at the same bar's close (engine.py: buy bar.close*(1+slip), sell bar.close*(1-slip)). The production runtime uses PaperBroker fill_mode='pending' (paper_broker.py docstring lines 4-12, and __init__ default fill_mode is 'immediate' but the runtime explicitly uses pending — see docstring 'production runtime uses pending'), which queues every signal and fills at the OPEN of the next trading bar (line 345 `bar.open if fill_basis=='open'`). So the price basis differs (close vs next-open) AND the timing differs (T+0 close vs T+1 open). These are two different trading systems. A strategy chosen because it backtests well will not behave the same when the live broker fills it one bar later at a different price.

**实盘影响**: The selector picks a strategy for live money based on close-fill backtests, but the live broker executes at next-open. The edge a strategy shows in backtest (especially momentum/breakout, which depend heavily on entry price relative to the breakout level) can fully or partially vanish at the next-open fill. Divergence is structural, not noise.

**建议修法**: Make the backtest engine model the SAME fill semantics as the live path: next-bar open fill, with T+1 enforced by construction (a signal generated on bar t cannot fill until bar t+1). Share one fill/price-basis implementation between BacktestEngine and PaperBroker so they cannot drift.

### [HIGH] No limit-up/down (涨停/跌停) or tradability gate — backtest fills trades that are impossible to execute live
- **位置**: `quanti/backtest/engine.py:217-341 (entire _execute_buy/_execute_sell)`  
- **模块**: backtest-engine

**问题**: Neither the engine nor commission/slippage models reference limit-up/down. A grep for 涨停/跌停/limit_up/limit_down across the repo shows zero handling in engine.py. The engine will happily BUY at a limit-up close (where live you cannot get filled — sealed at 涨停) and SELL at a limit-down close (where live there is no bid — sealed at 跌停). Stop-loss exits are the most dangerous case: the test (test_backtest.py line 130) drives a -20% crash bar and the stop-loss fills at that close; live, a -10% A-share limit-down bar is frequently unsellable that day, so the realized loss is far worse than the backtest's -8% stop.

**实盘影响**: Backtests overstate both entries (fills at limit-up that won't happen) and risk control (assumes stop-losses execute on limit-down days when they cannot). Live, a falling stock can lock limit-down for multiple sessions while the position bleeds well past the modeled -8% stop. Risk metrics (max_drawdown) are understated.

**建议修法**: Add a tradability gate before filling: skip/defer BUYs when the bar is at/above the limit-up (~+10%/+20% STAR/ChiNext, +5% ST relative to prev close) and SELLs when at/below limit-down. If the day is sealed, carry the order to the next bar (matching real queue behavior). Compute the limit from the previous close, honoring board-specific bands and ST.

### [HIGH] Per-stock position cap diverges: backtest deploys up to 95% of cash into one name, PaperBroker caps at 10% of total value
- **位置**: `quanti/execution/paper_broker.py:paper_broker.py:653,669 & 486,496 vs backtest/engine.py:240,247`  
- **模块**: paper-broker

**问题**: PaperBroker buy sizing applies size_cap = total_value * max_position_pct (10%) on every buy (paper_broker.py:653 for immediate, 486 for pending) and takes target_value = min(cash_cap, size_cap). The BacktestEngine._execute_buy applies NO per-stock cap: max_spend = portfolio.cash * 0.95 (engine.py:240) and the only upper bound is a hard 10000-share cap (engine.py:247). The engine's _risk.check (engine.py:164) only evaluates the ratio of the ALREADY-HELD position, so it gates re-entry into an existing oversized name but does not constrain the size of an initial buy. Net effect: for a fresh name the backtest can put ~95% of available cash into a single position while live caps it near 10%.

**实盘影响**: A strategy is validated in backtest holding a few large concentrated positions but trades ~10x smaller, far more diversified positions live. Return, drawdown, turnover, and capital utilization all differ materially from the backtest that justified going live — the live P&L will not resemble the backtested equity curve, and strategy ranking/selection done on backtests is invalid for the live sizing actually used.

**建议修法**: Make the two paths share one sizing function. Either apply the same total_value * max_position_pct cap inside BacktestEngine._execute_buy, or have the backtest call the same Sizer/cap logic the broker uses. At minimum, make max_position_pct an explicit shared input and assert in a test that an identical signal+portfolio produces the same quantity in BacktestEngine and PaperBroker(immediate).

### [HIGH] Backtest fill timing/price diverges from the live PaperBroker pending path
- **位置**: `quanti/backtest/engine.py:238, 253, 320`  
- **模块**: strategies

**问题**: The live agent runtime uses PaperBroker in 'pending' fill mode in production (paper_broker.py:8-13 docstring; runtime queues signals and try_fill_pending_orders fills at the NEXT trading bar's OPEN, paper_broker.py:344-347 `ref_price = bar.open`). The BacktestEngine instead fills at the CURRENT bar's CLOSE (engine.py:238/253/320). These are two different prices on two different bars for the identical signal. Additionally the sizing logic differs: the engine sizes by cash*0.95 and caps at 10000 shares/trade (engine.py:240,247) and applies VolumeImpactSlippage, while the PaperBroker sizes by signal.strength / sizer / max_position_pct (paper_broker.py:495-496, 668-669) and uses a flat slippage fraction (default 0.001). The Selector backtests with the close-fill engine (selector.py:119-135) to choose which strategy trades live with the open-fill broker.

**实盘影响**: A strategy validated and ranked in backtest is executed live under materially different fill price (open vs close), different position sizing, and a different slippage model. The realized P&L and risk profile will not match the backtest the decision was based on, undermining strategy selection and risk expectations with real QMT money.

**建议修法**: Unify the execution model: have the backtest engine and PaperBroker call a single shared fill routine that uses next-bar open, the same sizing (signal.strength/sizer/max_position_pct), and the same slippage/commission. Until unified, the Selector's rankings should be treated as not representative of live behavior.

### [HIGH] Factor overlay, fusion, threshold and industry-cap that pick LIVE buys are never exercised by the backtest
- **位置**: `signal_pipeline.py:95-189 (fuse_buy_signals), 192-211 (industry_cap), 214-218 (filter_by_threshold); used only at runtime.py:413-431; backtest/engine.py:75-192 has no factor/fuse path`  
- **模块**: signal-pipeline-factors

**问题**: BacktestEngine.run() (engine.py:75-192) only replays raw strategy.on_bar and applies RiskManager exits. It never calls compute_factor_panel, fuse_buy_signals, industry_cap, or filter_by_threshold. But the LIVE ensemble/LLM path ranks candidates by final_score = strat_w*ss + fb*factor_sigmoid + sb*sentiment, then drops everything below threshold (default 0.30) and caps positions per industry (runtime.py:413-431). The Selector chooses strategies based on their RAW alpha in the engine, yet live only trades the fused/factor-filtered subset. The composite weighting, factor_blend, sigmoid mapping (signal_pipeline.py:168), and 0.30 threshold are pure live-only logic with zero backtested PnL evidence.

**实盘影响**: The exact selection logic that decides what to buy with real money has never been measured. A factor_blend or threshold that silently destroys (or inverts) the strategies' edge would pass every backtest and only show up as live losses. Strategy ranking is also mis-specified: strategies are scored on alpha the live system won't actually harvest.

**建议修法**: Run the full pipeline (panel + fuse + industry_cap + threshold) inside the backtest at each rebalance date using point-in-time as_of, so the metrics that rank strategies and validate the system reflect what live actually trades. At minimum, backtest the fused-candidate construction end-to-end before going live.

### [HIGH] max_daily_trades never resets in live/paper — cap becomes a permanent lifetime kill after 20 trades
- **位置**: `quanti/risk/manager.py:39, 62-64, 128-134; runtime.py:582-743; paper_broker.py:542,592,731,780; qmt_broker.py:217`  
- **模块**: agent-cycle

**问题**: RiskManager._daily_trade_count is incremented on every fill (record_trade) and gates BUYs at max_daily_trades=20 (manager.py:63). reset_daily() is the ONLY thing that zeroes it, and grep shows it is called exclusively in the backtest engine (engine.py:145), NEVER in the agent runtime, PaperBroker, or QmtBroker. The broker is a long-lived singleton on app.state, so _daily_trade_count accumulates for the entire process lifetime. After 20 cumulative fills (potentially spread over many days), check() returns 'Daily trade limit reached' for every subsequent BUY until the server is restarted. SELLs are exempt (check() returns early for SELL), so the agent can still exit but can never re-enter. In backtest the counter resets each simulated day, so a strategy that trades a few times/day validates fine, then silently stops buying in live after ~20 lifetime trades.

**实盘影响**: After roughly 20 total fills the live agent stops opening new positions entirely and only ever sells — it slowly liquidates to cash and never redeploys, with the rejection logged as a benign 'daily limit'. The intended 20-trades-PER-DAY churn guard becomes a 20-trades-EVER guard. Backtests of the same strategy show normal trading, so this divergence is invisible until live capital quietly stops working.

**建议修法**: Call self._risk.reset_daily() at the start of each tick when the calendar day rolls over (track last-reset date on the broker or runtime), seeding the count from the day's actual venue trades for QmtBroker. Until then, treat the cap as known-broken in live. Add a test that runs >20 fills across two simulated days through PaperBroker and asserts the 21st BUY on day 2 is accepted.

### [HIGH] Backtest fills at today's close, PaperBroker at next-day open, QmtBroker at live quote — three different fill prices for the same signal
- **位置**: `quanti/backtest/engine.py:engine.py:253 (price=bar.close*(1+slip)), 320 (bar.close*(1-slip)); paper_broker.py:345 (bar.open); qmt_broker.py:251-252 (live last quote)`  
- **模块**: agent-cycle

**问题**: The same strategy signal fills at materially different prices/timings across the three execution paths the owner is validating together: (1) BacktestEngine fills BUY/SELL at the SAME bar's CLOSE (engine.py:253,320) — i.e. it assumes you transact at the close of the bar that produced the signal. (2) PaperBroker pending mode fills at the NEXT trading bar's OPEN (paper_broker.py:345, fill_basis='open'). (3) QmtBroker sizes and submits at the live realtime quote at tick time (qmt_broker.py:251-252,_latest_price). Slippage models also differ: backtest uses VolumeImpactSlippage (engine.py:68), both brokers use a flat self._slippage. The backtest's close-fill is itself optimistic/look-ahead-flavored (you can't reliably transact at the close you just observed), and neither broker reproduces it.

**实盘影响**: A strategy ranked as profitable in backtest (close-to-close) can be a loser live because A-share open-to-close gaps and the next-day-open fill move entry/exit prices against you, and the volume-impact penalty that disciplined the backtest disappears in live. The selector (which ranks strategies on backtest metrics) will send strategies live that were never validated under the price path they will actually trade at.

**建议修法**: Make the backtest fill at next-bar open (matching PaperBroker) so the validated price path equals the paper path, and apply the SAME slippage+commission model in all three. At minimum, document the divergence and run a paper-vs-backtest reconciliation report before any strategy goes live.

### [MEDIUM] No limit-up/down or suspension guard — fills assumed at open/close even when the price is untradeable
- **位置**: `quanti/execution/paper_broker.py:paper_broker.py:345-347,480,571 & backtest/engine.py:237,253,320`  
- **模块**: paper-broker

**问题**: Neither PaperBroker nor BacktestEngine checks whether the fill bar is at limit-up/limit-down or whether volume is zero (suspension/locked). Pending fills take bar.open unconditionally (paper_broker.py:345-347), immediate fills take latest close, and the backtest fills at bar.close +/- slippage. A grep for limit_up/限/涨停/0.1 shows no daily-price-limit logic and no prev_close-based limit computation in the execution path.

**实盘影响**: In A-shares a buy cannot fill when the stock is locked limit-up at the open, and a stop-loss/exit cannot fill when locked limit-down — yet both the backtest and the paper model book the fill at that price. Backtests therefore overstate the ability to enter strong-momentum names and (worse) to exit crashing names: the modelled stop-loss 'fills' at a price the market never offered, so live drawdowns on gap-down/limit-down names will be materially worse than the validated backtest. The paper broker (the live-path model) inherits the same optimism.

**建议修法**: Add a tradeability check shared by both engines: skip/defer a fill when the fill bar has zero volume (suspended) or is at the daily limit in the adverse direction (open == high == low at limit-up for buys; at limit-down for sells), using prev_close and the per-board limit (10% main board, 20% ChiNext/STAR, etc.). For pending orders, leave them pending (re-queue) rather than booking a phantom fill.

### [MEDIUM] Strategy-exit replay in PaperBroker uses default params, ignoring the goal's tuned params — exit logic diverges from the entry strategy
- **位置**: `quanti/execution/paper_broker.py:836-868 (_compute_strategy_exits: strat.init(getattr(strat,'params',{}) or {}))`  
- **模块**: agent-cycle

**问题**: _compute_strategy_exits decides whether a holding's owning strategy now says SELL by instantiating a fresh strategy and calling strat.init(getattr(strat, 'params', {}) or {}) — i.e. with the class default params, NOT goal.params. The entries, however, were generated with the goal's params (runtime.py:680,706,725 strategy.init(goal.params)). For a strategy like MACrossStrategy the cross thresholds come entirely from config (short_period/long_period, ma_cross.py:16-17); if the goal tuned short_period=10/long_period=30, entries use 10/30 but the exit replay uses the 5/20 defaults. The death-cross that the EXIT gate looks for is computed on different MAs than the golden-cross that opened the position. The backtest, by contrast, runs one strategy instance with the goal params for both entry and exit, so entry/exit are coherent there.

**实盘影响**: Positions opened on tuned parameters are exited (or not exited) on default parameters, so the structure-based 'strategy-coherent exit' the design promises is incoherent live/paper: holdings can be force-sold by a default-param death cross that the real (tuned) strategy never signaled, or held past a tuned exit the defaults miss. The exit behavior was never validated this way in backtest.

**建议修法**: Persist the params used at entry (per position or per strategy) and pass them to strat.init() in _compute_strategy_exits, or thread the active goal.params through the broker so entry and exit replays use identical configuration.

---

## 指标/选股误排 (metric-selection) — 5 条

### [HIGH] OOS metrics annualize a ~21-calendar-day (≈13-15 trading bar) window, inflating Sharpe/return used to rank strategies
- **位置**: `quanti/backtest/metrics.py:19, 31`  
- **模块**: strategies

**问题**: Walk-forward folds default to test_days=21 CALENDAR days (selector.py:110, walk_forward.py make_folds uses timedelta(days=test_days) at line 88-90), so each OOS slice contains only ~13-15 trading bars. compute_metrics then annualizes: annual_return = (1+total_return)**(252/n_days)-1 with n_days≈14 → exponent ≈18×, and sharpe = excess_daily/std*sqrt(252) computed from ~13 daily returns (metrics.py:19,31). Annualizing a two-week window produces wildly amplified, high-variance numbers. selector._score and pick_topk then rank/weight strategies primarily on these OOS annual_return and oos_sharpe (selector.py:213-216, 252-259, 296-300), and softmax with temp=0.5 turns small noisy Sharpe differences into large weight differences.

**实盘影响**: Strategy selection and ensemble weighting are driven by statistically unstable, over-annualized metrics from tiny samples. A strategy that got lucky in one 2-week fold can be annualized to a huge Sharpe/return and promoted (or heavily weighted) for live trading, sending real money to an overfit/noise-driven pick.

**建议修法**: Either (a) pool all OOS test-slice returns across folds into one return series and compute Sharpe/annualization once on the combined sample, or (b) use far longer test windows, or (c) rank on per-period (non-annualized) statistics with a minimum-sample guard. Also measure test_days in trading days, not calendar days, and require a minimum number of OOS bars/trades before a strategy is eligible.

### [HIGH] OOS annual return annualizes ~14-trading-day windows to absurd magnitudes — the 933% bug — and this dominates strategy ranking
- **位置**: `quanti/backtest/metrics.py:19`  
- **模块**: selector-walkforward

**问题**: compute_metrics computes annual_return = (1 + total_return) ** (trading_days / max(n_days, 1)) - 1 with trading_days=252. In walk-forward, compute_metrics is called on the OOS slice only (walk_forward.py:151), and each fold's test window is test_days=21 CALENDAR days (selector.py:110, make_folds at walk_forward.py:88-90). A 21-calendar-day window contains only ~14-15 trading days, so n_days≈14 and the annualization exponent is 252/14 ≈ 18. A benign +15% over the fold becomes 1.15^18 - 1 ≈ +1140%; +13% becomes ~+790%; this is exactly the '933% OOS annual return' the owner observed. run_walk_forward then sets oos_annual_return = mean of these per-fold annualized returns (walk_forward.py:163,173,185). In selector._score this OOS number feeds return_score = ann_return/target capped at +1.5 (selector.py:253,266); with target_annual_return=0.20 essentially every strategy with any positive fold pins return_score to its +1.5 cap, so the return component stops discriminating and instead measures which strategy got a lucky short-window pop. For HIGH risk tolerance w_ret=1.2 (selector.py:280) this noise-amplified term carries the largest return weight in the composite, misranking strategies.

**实盘影响**: The strategy chosen to trade real money is selected largely by which candidate happened to have the largest short-window gain, exponentially amplified. A strategy that got +18% in one lucky 2-week fold outranks a steady +6%/fold strategy. Live performance will not resemble the backtest ranking; expect the 'winner' to mean-revert and lose money.

**建议修法**: Do not annualize sub-quarter windows. Either (a) rank folds on raw (non-annualized) test-window total_return and only annualize the aggregate across all folds combined, or (b) compute annual_return from the geometric mean of DAILY returns (mean_daily_ret compounded to 252) which is far less sensitive to window length, or (c) require a minimum n_days (e.g. >= 40 trading days per fold) and lengthen test_days. At minimum, cap/clip annualized fold returns before averaging so a single ~1000% fold cannot dominate the mean.

### [HIGH] Live capital allocation (softmax weights in pick_topk) is driven by Sharpe estimated from ~14 daily returns — pure noise
- **位置**: `quanti/agent/selector.py:213-227`  
- **模块**: selector-walkforward

**问题**: pick_topk builds per-strategy weights from oos_sharpe via softmax with temp=0.5 (selector.py:213-227). oos_sharpe is the mean across folds of compute_metrics' sharpe (walk_forward.py:165,187), and each fold's Sharpe is computed from the returns series of a single ~14-trading-day OOS slice (metrics.py:30-31: excess_daily/returns.std()*sqrt(252)). A Sharpe estimated from ~13 daily return observations has an enormous standard error (roughly +/-1 on the annualized figure just from sampling), so the ranking of oos_sharpe across strategies is dominated by sampling noise, not skill. The softmax temperature of 0.5 then turns small noise differences into large weight differences (a 0.5-vs-1.0 Sharpe gap -> exp(2) vs exp(1) ≈ 73%/27% split). Because n_folds is only 3 (selector.py:108) the mean does little to reduce this variance.

**实盘影响**: Real money is split across strategies by a metric that is essentially random at this sample size. The biggest live allocation routinely goes to whichever strategy was luckiest over the last ~6 weeks of OOS data rather than the most robust one, and the allocation will swing wildly cycle-to-cycle.

**建议修法**: Estimate Sharpe from the POOLED OOS daily returns across all folds (concatenate the OOS daily-return series, then one Sharpe) instead of averaging tiny per-fold Sharpes. Increase folds/test_days so each Sharpe rests on enough observations. Raise the softmax temperature (less peaky) or weight by a more stable quantity (e.g. pooled OOS total return with a drawdown penalty). Consider shrinking weights toward equal-weight given the small sample.

### [MEDIUM] OOS consistency is the coefficient-of-variation of the absurdly-annualized fold returns, not of real return stability
- **位置**: `quanti/agent/walk_forward.py:163-181`  
- **模块**: selector-walkforward

**问题**: _aggregate computes consistency from rets = per-fold annual_return values (walk_forward.py:163), i.e. the same exponentially-annualized numbers flagged above, via cov = std_ret/abs(mean_ret); consistency = 1 - cov (walk_forward.py:173-181). Because annualization is a strongly nonlinear function of the per-fold total return, the spread (std) of annualized fold returns is dominated by the annualization curvature, not by how stable the underlying strategy is. Two strategies with identical raw fold returns but folds of slightly different trading-day counts (see calendar-day issue) get different annualized spreads and thus different 'consistency'. The consistency bonus (w_consistency=0.4, selector.py:287,299) therefore rewards an artifact. The mean_ret near-zero fallback at walk_forward.py:177-178 (-|std| of annualized returns) is likewise on the wrong scale — std of ~1000%-magnitude numbers can be huge, flooring consistency at -1 almost always.

**实盘影响**: The consistency tie-breaker that is supposed to prefer steady strategies over boom-bust ones is measuring annualization noise, so it can prefer the more erratic strategy. Contributes to sending the wrong strategy live.

**建议修法**: Compute consistency from RAW per-fold test-window total returns (or per-fold daily-return means), not annualized returns. Apply the near-zero fallback on that same raw scale.

### [MEDIUM] A fold with as few as 2 trading days is accepted and annualized with exponent 126
- **位置**: `quanti/agent/walk_forward.py:147-151`  
- **模块**: selector-walkforward

**问题**: run_walk_forward only skips a fold when len(oos_curve) < 2 (walk_forward.py:147). A fold with exactly 2 trading days (e.g. one straddling a holiday week, made possible by the calendar-day sizing above, or a thinly-traded capped universe where bars are sparse) passes the guard. compute_metrics then annualizes with exponent 252/2 = 126 (metrics.py:19): a single +3% day becomes 1.03^126 - 1 ≈ +4100%. That fold's return then enters the mean for oos_annual_return and its Sharpe (computed from a single pct_change observation, returns.std() on 1 point is NaN/0 -> sharpe 0) enters the mean. One such fold can swamp the 3-fold average.

**实盘影响**: A single short/sparse fold can make a mediocre strategy appear to be the top performer and win the live slot. The smaller/sparser the traded universe (capped at selector_max_universe), the more likely this fires.

**建议修法**: Require a meaningful minimum trading-day count per fold (e.g. len(oos_curve) >= ~10) before accepting it, and drop folds below that from the aggregate. Combine with trading-day-based fold construction so window length is guaranteed.

---

## 其它正确性 (correctness) — 2 条

### [HIGH] volume_breakout: unguarded divide-by-zero on prior close can abort a live screening cycle
- **位置**: `screeners/volume_breakout.py:24-25`  
- **模块**: screeners

**问题**: screen() computes prev_close = bars[-2].close then price_change = (latest.close - prev_close) / prev_close with NO zero/None guard. The volume side is guarded (avg_vol == 0 -> return 0.0, line 33) but the price side is not. A prior close of 0 (suspended/newly-listed/garbage bar, or a bad row from the data feed) raises ZeroDivisionError. The other three screeners only divide by means of close/high that are practically non-zero, but this one divides by a single arbitrary bar value. Note ma_trend.py:34-35 and new_high.py:30 also divide by ma_mid/ma_long/prev_high without explicit guards, though those are means/maxes so far less likely to hit zero.

**实盘影响**: In the MCP run_screener path (quanti/mcp_server.py:397-401) the per-code screen() call is NOT wrapped in try/except, so a single code with a 0 prior close throws and aborts the ENTIRE screening call — no candidates returned for that live cycle. The agent runtime path (runtime.py:337-338) swallows it per-code, so the symptom differs by entry point, but the underlying bug is the same and the worst path crashes the cycle.

**建议修法**: Guard prev_close before dividing: `if prev_close <= 0: return 0.0`. Apply the same defensive guard to ma_mid/ma_long (ma_trend) and prev_high (new_high) so no screener can throw on degenerate bars.

### [MEDIUM] Codes missing from the factor panel are scored as 'median attractive' (factor_score 0 → sigmoid 0.5) instead of penalized/excluded
- **位置**: `signal_pipeline.py:84-92 (_factor_score returns 0.0 when code absent/NaN), 166-168 (fs_norm = sigmoid(0.0)=0.5), 178 (final blend)`  
- **模块**: signal-pipeline-factors

**问题**: _factor_score returns 0.0 when a code is absent from the panel or its composite is NaN (signal_pipeline.py:88,90-92). compute_factor_panel drops any code with <21 bars or no data (cross_sectional.py:197). For such a dropped/thin-history code that still produced a strategy BUY, fuse_buy_signals maps factor_score 0.0 through sigmoid to fs_norm=0.5 (line 168), identical to a stock whose composite is genuinely 0 (cross-sectionally average). With default factor_blend=0.5 and a strong strategy signal, final = 0.5*1.0 + 0.5*0.5 = 0.75, easily clearing the 0.30 threshold. The factor overlay is supposed to gate on relative attractiveness, but a stock the model knows nothing about gets a neutral pass rather than being held out.

**实盘影响**: Thin-history / data-gap names (recent IPOs that slipped the universe filter, codes with sync gaps) bypass the factor screen and get bought on strategy signal alone, at full neutral factor weight — exactly the illiquid/idiosyncratic names the factor layer exists to avoid. Live buys riskier stocks than the factor model intends.

**建议修法**: Distinguish 'absent from panel' from 'composite == 0'. For codes not in the panel (or NaN composite), either drop the candidate, or treat missing factor as a penalty (e.g. fs_norm well below 0.5) rather than neutral. Track and log how many fused buys had no factor coverage.

---

## 已证伪(误报，已过滤) — 17 条

- **Annualizing ~21-day walk-forward windows produces absurd returns that mis-rank which strategy goes live** (`quanti/backtest/metrics.py`, backtest-engine) — REFUTED. The finding correctly describes the code mechanics (metrics.py:19 annualizes via (1+total_return)**(252/max(n_days,1))-1; walk_forward.py:73 test_days=21, :102-103 slices OOS to that window, :151 calls compute_metrics on a ~15-day slice, :163/173/185 means per-fold annualized returns into o
- **Buy slippage/commission paid from cash but position immediately marked at un-slipped close — small equity self-inflation per round trip** (`quanti/backtest/engine.py`, backtest-engine) — REFUTED. The finding describes the code mechanics accurately but its claimed hazards do not exist; it even concedes the core accounting is correct ("low"/"negligible"/"cosmetic").

Facts verified in C:/Users/HuaWenbo/Documents/GitHub/quanti/quanti/backtest/engine.py:
- Buy: price = bar.close*(1+real
- **Pending-order dedup silently drops legitimate signals (re-entries and same-tick BUY+SELL pairs)** (`quanti/execution/paper_broker.py`, paper-broker) — REFUTED — this is a design-preference/observability nit, not a genuine correctness or live-safety bug.

1) The central claim — "Backtest has no such dedup ... so signal-to-fill mapping differs between backtest and live" — is a category error. The backtest engine (C:/Users/HuaWenbo/Documents/GitHub/q
- **blocked_prefixes (ST/*ST) is dead config in RiskManager — never checked in check()** (`quanti/risk/manager.py`, risk-manager) — The finding's facts are accurate but it does not constitute a genuine correctness/live-safety bug per the audit's bar — it is a dead-config / documentation nit.

VERIFIED FACTS (the finding is literally correct here): RiskConfig.blocked_prefixes is defined at quanti/risk/manager.py:20 and grep acros
- **VolTargetSizer can return weight above max_pct when vol is near-zero, and floor*s can override max_pct cap** (`quanti/risk/sizer.py`, risk-manager) — The finding is self-refuting. All three of its mechanical claims fail to demonstrate any actual breach:

1. Near-zero-vol branch (sizer.py:148) returns `self._max_pct * s` with s clamped to [0,1] (line 124), so it is at most max_pct. The finding itself says "which is fine."

2. The main return (size
- **RSI uses simple mean of gains/losses instead of Wilder smoothing — divergence from compute_rsi and standard RSI** (`strategies/rsi_ob_os.py`, strategies) — REFUTED. The finding is a methodology/convention nit, not a correctness or live-safety bug, and its headline claim is factually wrong.

Verified facts:
- strategies/rsi_ob_os.py:34-39 (_rsi) computes RSI as 100 - 100/(1 + mean(gains)/mean(losses)) using np.mean over the window — a simple-moving-aver
- **Fold windows are sized in calendar days, so each fold covers a different number of trading days — inconsistent annualization and uneven OOS samples across folds** (`quanti/agent/walk_forward.py`, selector-walkforward) — The finding's central claim is a misreading. It asserts that calendar-day-sized fold windows corrupt annualization because "compute_metrics annualizes by 252/n_days (metrics.py:19)" and so the "SAME raw return is annualized differently in different folds." But n_days is NOT calendar days: metrics.py
- **Selector scores under VolumeImpactSlippage + risk exits but the IS baseline and OOS folds may diverge from the live execution path on lot/cap/T+1 details** (`quanti/backtest/engine.py`, selector-walkforward) — REFUTED — this is a self-acknowledged verification TODO, not a confirmed correctness/safety bug. The auditor itself labels it severity=low and writes "This is a verification item, not a confirmed divergence" and conditions its impact on "IF live sizing differs."

What is factually true: the three bu
- **MCP run_screener does not isolate per-code screener exceptions; one bad code aborts the whole call** (`quanti/mcp_server.py`, screeners) — The finding's raw factual claim is correct but its severity/category (high / backtest-vs-live, live-money impact) is a misreading. Refuted as a live-safety/correctness bug.

What is accurate: mcp_server.py:397-401 does run `bars = ctx.provider.get_daily_bars(c, ...); score = scr.screen(c, bars)` ins
- **ma_trend forces a 0.1 score floor on every bullish-aligned stock, defeating separation-based ranking** (`screeners/ma_trend.py`, screeners) — REFUTED — this is a design/tuning choice, not a correctness or live-safety bug.

Code: C:/Users/HuaWenbo/Documents/GitHub/quanti/screeners/ma_trend.py. The 0.1 floor at line 43 (`return round(max(score, 0.1), 3)`) is reachable ONLY after three hard qualification gates have each returned 0.0 (skip):
- **Screeners decide on bars[-1] with end=date.today(); a same-day forming bar would be look-ahead** (`quanti/agent/runtime.py`, screeners) — The finding's load-bearing claims are refuted by the actual code.

1) NO fill-price look-ahead. The finding's core harm — selecting/trading on today's unfinished close at an "unrealizable" price — does not occur. PaperBroker queues every signal and fills at the NEXT trading bar's open: try_fill_pend
- **new_high computes period_high but never uses it; intended high-vs-high breakout check appears dropped** (`screeners/new_high.py`, screeners) — REFUTED — false positive (dead-code nit, not a correctness/live-safety bug).

The factual claim is accurate: at C:/Users/HuaWenbo/Documents/GitHub/quanti/screeners/new_high.py:22, `period_high = max(b.high for b in bars[-self.period:])` is computed and never referenced again (the gate at line 26 use
- **Industry-neutralization demeans but does not re-standardize, distorting equal-weight composite** (`cross_sectional.py`, signal-pipeline-factors) — REFUTED as a correctness/live-safety bug. The finding's statistics are accurate but it describes a methodology preference, not a genuine hazard.

Code reviewed: quanti/factors/cross_sectional.py:142-168 (_zscore, _industry_demean), 216-242 (pipeline + composite); consumers quanti/agent/runtime.py:41
- **Selector ranks strategies on raw alpha but warns of exit-policy match only — fusion/threshold gap means OOS Sharpe may not reflect tradable returns** (`selector.py`, signal-pipeline-factors) — REFUTED as a forward-looking modeling nit, not a genuine correctness/live-safety bug. The mechanics the finding describes are factually accurate: pick_topk softmax-weights the ensemble by oos_sharpe (quanti/agent/selector.py:213-227); those Sharpes come from BacktestEngine runs that gate signals onl
- **Pending-mode BUYs over-allocate: 80% total-cap and 10% single-cap are checked only against FILLED positions, not in-flight queued orders** (`quanti/execution/paper_broker.py`, agent-cycle) — REFUTED. The finding's mechanical observations are accurate but its safety conclusion is wrong: the 80% total and 10% single caps ARE actually enforced before any money moves, so the assembled portfolio cannot exceed them.

What is true (verified): In pending mode the queue-time gate (paper_broker.p
- **Stop-loss / exits in pending mode only QUEUE sells for next-day open — comment claims they free cash 'first'; -8% stop is delayed a full session** (`quanti/agent/runtime.py`, agent-cycle) — REFUTED — the finding misreads which broker is the live path, and its only accurate sub-claim is a documented scaffold gap, not a silent hazard.

HEADLINE CLAIM IS A MISREADING. The finding asserts that in "production default" the stop-loss only QUEUES a sell for next-day open, delaying the -8% stop
- **QmtBroker pending reconciliation matches venue orders via entry_strategy, but submit mirrors are written terminal — pending rows can be orphaned/mis-keyed** (`quanti/execution/qmt_broker.py`, agent-cycle) — REFUTED. The finding rests on two claims; both fail against the actual code.

CLAIM (1) "venue id in entry_strategy corrupts strategy-exit attribution" — FALSE (column conflation). There are TWO distinct entry_strategy columns: positions.entry_strategy (database.py:122) and orders.entry_strategy (da