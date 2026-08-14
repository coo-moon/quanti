"""Offline LLM decision-layer evaluation — run the replay against the real DB.

Usage:
    python scripts/llm_eval.py --days 30 --k 5 --out data/llm_eval_2026-08-14.json

Requires DEEPSEEK_API_KEY (default provider=deepseek). Saves a machine-
readable report and prints a human summary. Never trades; the account DB is
only read (plus an optional --log-decision audit entry).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Standalone script: make the repo root importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import json
import logging
import os
import sys
from datetime import date

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _summarize(report: dict) -> str:
    lines = [
        f"离线 LLM 评估 — {report['n_days']} 天, k={report['k']}, LLM 失败 {report['n_llm_error_days']} 天",
    ]
    for h, s in report["summary"].items():
        lines.append(f"  {h}: baseline={_pct(s['baseline_mean'])} "
                     f"llm={_pct(s['llm_mean'])} "
                     f"candidates={_pct(s['candidates_mean'])} "
                     f"| beat-rate baseline={_pct(s['baseline_beat_rate'])} "
                     f"llm={_pct(s['llm_beat_rate'])}")
    return "\n".join(lines)


def _pct(v) -> str:
    return f"{v * 100:+.1f}%" if v is not None else "—"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--end", default=None, help="最后评估日 YYYY-MM-DD(默认今天)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--stride", type=int, default=5, help="评估日间隔(交易日)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--max-codes", dest="max_codes", type=int, default=60)
    ap.add_argument("--horizon", type=int, nargs="+", default=[5, 10])
    ap.add_argument("--out", default=None, help="报告 JSON 输出路径")
    ap.add_argument("--provider", default="deepseek",
                    help="llm_provider: deepseek | anthropic | openai_compat")
    ap.add_argument("--model", default=None)
    ap.add_argument("--log-decision", action="store_true",
                    help="把摘要写进决策日志(审计流可见)")
    args = ap.parse_args()

    from quanti.agent.llm_eval import evaluate
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider

    end = date.fromisoformat(args.end) if args.end else None
    account = os.environ.get("QUANTI_ACCOUNT", "paper")
    db = Database(f"data/{account}.db", market_db_path="data/market.db")
    db.initialize()
    provider = DataProvider(db)

    try:
        from quanti.agent.llm_runtime import build_llm_client
        llm = build_llm_client({"llm_provider": args.provider,
                                "llm_model": args.model})
    except Exception as e:  # noqa: BLE001
        logger.error("LLM 客户端构建失败(检查 API key): %s", e)
        sys.exit(2)

    report = evaluate(db, provider, llm, end=end, n_days=args.days,
                      stride=args.stride, k=args.k,
                      horizons=tuple(args.horizon), max_codes=args.max_codes)
    out = args.out or f"data/llm_eval_{date.today().isoformat()}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(_summarize(report))
    logger.info("报告已写入 %s", out)
    if args.log_decision:
        db.log_decision("llm_eval",
                        f"离线 LLM 评估完成: {report['n_days']} 天,摘要见 {out}",
                        details=report["summary"])
    # LLM 大面积失败说明评估不可信 — 用退出码 2 表达,别让坏数据假装结论。
    if report["n_llm_error_days"] > max(1, len(report["days"]) // 2):
        logger.error("LLM 失败 %d/%d 天,评估不可信", report["n_llm_error_days"],
                     len(report["days"]))
        sys.exit(2)


if __name__ == "__main__":
    main()

