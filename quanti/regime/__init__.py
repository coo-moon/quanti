"""市场 regime 快照:宽度指标(规则层)+ 时事面 LLM 解读(叙事层)。

- :mod:`quanti.regime.breadth` — 全A股宽度/轮动/资金指标,纯数据,可复现
- :mod:`quanti.regime.news`    — 新闻联播 + 财经快讯,尽力而为
- :mod:`quanti.regime.report`  — 组装 → DeepSeek 深度思考 → 落库
- :mod:`quanti.regime.prompt`  — 已落库快照 → tick 日志行 / LLM 上下文段(只读)

**不产生交易信号**。快照每天 17:30 由后台生成一次;agent tick 第一步只
*读* 它(~1ms)写决策日志,并在 ``regime_in_prompt`` 打开时把其中的客观
指标拼进裁判 LLM 的上下文 —— 作为环境描述,不作仓位开关。为什么只到这一步、
哪些字段被刻意剔除,见 :mod:`quanti.regime.prompt` 的模块文档。

(2026-07-29 起本包取代了原 ``quanti.agent.regime``:那套用 universe 前 120
个代码的等权合成指数 + Kaufman 效率比,在生产里连续 65 条日志全是 high_vol、
零辨别力,已删除。)
"""
