"""市场 regime 快照:宽度指标(规则层)+ 时事面 LLM 解读(叙事层)。

- :mod:`quanti.regime.breadth` — 全A股宽度/轮动/资金指标,纯数据,可复现
- :mod:`quanti.regime.news`    — 新闻联播 + 财经快讯,尽力而为
- :mod:`quanti.regime.report`  — 组装 → DeepSeek 深度思考 → 落库

纯观测(observe-only):不产生交易信号,不接执行链路。

与 :mod:`quanti.agent.regime` 的区别(名字像,用途不同,别混):

* ``quanti.agent.regime`` — agent 决策循环内部用的 regime 判定(等权合成
  指数 + Kaufman 效率比 + 波动分位),输出一个 label 供 agent 倾斜,跑在
  每个 tick 上。
* 本包 — 给**人**看的每日快照报告:全市场宽度、板块轮动、资金、时事面,
  外加 LLM 叙事与仓位框架,每天 17:30 跑一次并落库供 UI 展示。
"""
