"""Manual smoke test: REAL news → REAL LLM sentiment scoring.

This is the one piece the unit tests stub out (they inject a fake LLM). Run it
once against a live provider to confirm the scores are sane. Works with either
Anthropic or DeepSeek — it picks whichever API key is set.

Prerequisites (pick ONE provider):
    # DeepSeek (OpenAI-compatible, no extra install — uses httpx):
    export DEEPSEEK_API_KEY=sk-...
    # ...or Anthropic:
    pip install -e '.[llm]'
    export ANTHROPIC_API_KEY=sk-ant-...

Run:
    python scripts/smoke_llm_sentiment.py

Cost: one batched scoring call for the real symbols + one synthetic sanity
call — a few cents.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

# Import the quanti package sitting next to this script (works from a worktree
# or a fresh clone without relying on the editable install location).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quanti.agent.sentiment import (  # noqa: E402
    SentimentConfig,
    _score_with_llm,
    score_candidates,
)
from quanti.data.database import Database  # noqa: E402

SYMBOLS = ["600519", "000001", "300750"]  # 茅台 / 平安银行 / 宁德时代


def make_client():
    """Pick a provider from whichever API key is present. Returns
    (client, model_override or None)."""
    if os.environ.get("DEEPSEEK_API_KEY"):
        from quanti.agent.openai_compat import DEEPSEEK_DEFAULT_MODEL, DeepSeekLLMClient
        return DeepSeekLLMClient(), DEEPSEEK_DEFAULT_MODEL
    if os.environ.get("ANTHROPIC_API_KEY"):
        from quanti.agent.llm_runtime import AnthropicLLMClient
        return AnthropicLLMClient(), None  # use SentimentConfig's default model
    raise SystemExit(
        "Set DEEPSEEK_API_KEY or ANTHROPIC_API_KEY in the environment first.")


def main() -> int:
    client, model = make_client()
    cfg = SentimentConfig(max_codes=len(SYMBOLS),
                          **({"model": model} if model else {}))
    print(f"provider: {type(client).__name__}  model: {cfg.model}")

    db = Database(str(Path(tempfile.mkdtemp()) / "smoke.db"))
    db.initialize()

    print(f"\nScoring real news for {SYMBOLS} ...")
    scores = score_candidates(db, SYMBOLS, client, cfg=cfg)
    today = date.today().isoformat()
    print("\n=== real-news scores ===")
    for code in SYMBOLS:
        row = db.get_news_sentiment(code, today)
        n_news = row["n_news"] if row else 0
        reason = row["reason"] if row else ""
        print(f"  {code}: score={scores.get(code, 0.0):+.2f}  "
              f"(n_news={n_news})  {reason}")

    # Sign sanity: obviously bullish vs obviously bearish synthetic headlines.
    print("\n=== sign sanity (synthetic headlines) ===")
    items = [
        {"code": "BULL", "headlines": [
            "公司发布业绩预增公告，预计全年净利润同比增长300%，创历史新高",
            "获政府大额补贴，并中标多个重大项目"]},
        {"code": "BEAR", "headlines": [
            "公司被证监会立案调查，涉嫌财务造假",
            "实控人大幅减持，公司发布业绩预亏公告"]},
    ]
    out = _score_with_llm(client, items, cfg)
    for code, (sc, reason) in out.items():
        print(f"  {code}: {sc:+.2f}  {reason}")

    bull = out.get("BULL", (0.0, ""))[0]
    bear = out.get("BEAR", (0.0, ""))[0]
    ok = bull > 0 > bear
    print(f"\nsign check: BULL={bull:+.2f} > 0 > BEAR={bear:+.2f}  -> "
          f"{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
