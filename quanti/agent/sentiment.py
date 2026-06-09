"""News / sentiment analyst — the system's only non-price input.

The rule-ensemble + factor pipeline is 100% price/volume. A-share moves are
heavily news- and sentiment-driven, so this module adds a *judgment overlay*:

  1. For the top-N candidate codes, fetch recent news headlines (AkShare).
  2. Hand the headlines to an LLM, which scores each stock's near-term
     sentiment in [-1, 1] (one batched call for all codes — cheap + cacheable).
  3. Cache each score in `news_sentiment` keyed by (code, trading-date) so we
     never re-fetch or re-score the same stock twice in a day.

The scores feed `signal_pipeline.fuse_buy_signals(..., sentiment_scores=...,
sentiment_blend=...)` as a third blend term alongside strategy + factor.

Design invariants (mirrors `llm_runtime`):
  * AkShare is imported lazily — the rest of the system runs without news.
  * Any failure (no akshare, no network, no LLM, bad rows) degrades to a
    neutral 0.0 for that code; it NEVER raises into the agent cycle.
  * The LLM only *scores* news; it cannot pick or size positions here. That
    stays with the deterministic pipeline + RiskManager.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

from quanti.agent.llm_runtime import DEFAULT_MODEL, LLMClient
from quanti.data.database import Database

logger = logging.getLogger(__name__)


@dataclass
class SentimentConfig:
    model: str = DEFAULT_MODEL
    max_tokens: int = 1024
    temperature: float = 0.0       # scoring should be near-deterministic
    max_codes: int = 30            # hard cap on stocks scored per tick (cost)
    max_news_per_code: int = 8     # headlines fed to the LLM per stock
    lookback_days: int = 7         # only consider news this fresh


SENTIMENT_SYSTEM = """You are a news-sentiment analyst for A-share (China) stocks. \
For each stock you are given recent headlines. Judge the NEAR-TERM (days to weeks) \
price sentiment and output a score in [-1, 1]:
  +1 = clearly bullish news, 0 = neutral / no real signal, -1 = clearly bearish.

Heuristics:
  * Negative: 立案/调查/处罚, 减持, 业绩下滑/预亏, 商誉减值, 质押爆仓, 退市风险, 诉讼.
  * Positive: 业绩预增, 中标/大单, 增持/回购, 重组利好, 政策扶持, 机构调研密集.
  * Be skeptical of vague promotional headlines — score them near 0.
Call `submit_sentiment` exactly once with one entry per stock."""

SENTIMENT_TOOL: list[dict] = [{
    "name": "submit_sentiment",
    "description": "Return one near-term sentiment score per stock. "
                   "Call exactly once with all stocks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "score": {"type": "number",
                                  "minimum": -1, "maximum": 1},
                        "reason": {"type": "string", "maxLength": 60},
                    },
                    "required": ["code", "score"],
                },
            },
        },
        "required": ["scores"],
    },
}]


# ----------------------------------------------------------- news fetch

def _first_present(columns: Any, candidates: list[str]) -> str | None:
    cols = set(columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def fetch_recent_news(
    code: str,
    *,
    limit: int = 8,
    lookback_days: int = 7,
    as_of: date | None = None,
) -> list[dict]:
    """Fetch recent headlines for `code` via AkShare `stock_news_em`.

    Returns a list of {"title", "time", "source"} dicts (newest first),
    filtered to roughly the lookback window. Degrades to [] on any failure
    (akshare missing, network down, unexpected schema) — never raises.
    """
    try:
        import akshare as ak  # type: ignore
    except ImportError:
        logger.debug("akshare not installed; skipping news for %s", code)
        return []
    try:
        df = ak.stock_news_em(symbol=code)
    except Exception as e:  # network / upstream / symbol issues
        logger.debug("stock_news_em failed for %s: %s", code, e)
        return []
    if df is None or len(df) == 0:
        return []

    title_col = _first_present(df.columns, ["新闻标题", "标题", "title"])
    time_col = _first_present(df.columns, ["发布时间", "时间", "datetime", "date"])
    source_col = _first_present(df.columns, ["文章来源", "来源", "source"])
    if title_col is None:
        return []

    as_of = as_of or date.today()
    cutoff = as_of - timedelta(days=lookback_days)

    out: list[dict] = []
    for _, row in df.iterrows():
        title = str(row.get(title_col, "")).strip()
        if not title:
            continue
        ts_raw = str(row.get(time_col, "")) if time_col else ""
        # Best-effort recency filter; if the timestamp won't parse, keep it
        # rather than silently dropping potentially-relevant news.
        if ts_raw:
            try:
                import pandas as pd
                d = pd.to_datetime(ts_raw, errors="coerce")
                if d is not None and not pd.isna(d) and d.date() < cutoff:
                    continue
            except Exception:
                pass
        out.append({
            "title": title,
            "time": ts_raw,
            "source": str(row.get(source_col, "")) if source_col else "",
        })
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------- LLM scoring

def _score_with_llm(
    llm: LLMClient,
    items: list[dict],
    cfg: SentimentConfig,
) -> dict[str, tuple[float, str]]:
    """One batched call: items = [{"code", "headlines": [str]}]. Returns
    { code → (score, reason) }. Missing/garbled output → empty dict."""
    lines = ["请为下列每只股票的近期新闻打一个情绪分(-1 到 1):", ""]
    for it in items:
        lines.append(f"## {it['code']}")
        for h in it.get("headlines", []):
            lines.append(f"- {h}")
        lines.append("")
    user = "\n".join(lines)

    system = [{
        "type": "text", "text": SENTIMENT_SYSTEM,
        "cache_control": {"type": "ephemeral"},
    }]
    resp = llm.create_message(
        model=cfg.model,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=SENTIMENT_TOOL,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
    )
    out: dict[str, tuple[float, str]] = {}
    for block in resp.get("content", []) or []:
        if block.get("type") == "tool_use" and block.get("name") == "submit_sentiment":
            for s in (block.get("input", {}) or {}).get("scores", []) or []:
                code = str(s.get("code", "")).strip()
                if not code:
                    continue
                try:
                    score = float(s.get("score", 0.0))
                except (TypeError, ValueError):
                    score = 0.0
                out[code] = (max(-1.0, min(1.0, score)), str(s.get("reason", "")))
    return out


# ----------------------------------------------------------- orchestrator

def score_candidates(
    db: Database,
    codes: list[str],
    llm_client: LLMClient | None,
    *,
    as_of: date | None = None,
    cfg: SentimentConfig | None = None,
    news_fetcher: Callable[..., list[dict]] | None = None,
) -> dict[str, float]:
    """Return { code → sentiment ∈ [-1, 1] } for `codes`, cache-first.

    Flow per code: cache hit → use it. Else fetch news; no news → cache a
    neutral 0.0. Codes that have news are scored together in ONE LLM call,
    then each result is cached. If no LLM client is available, the unscored
    codes return 0.0 for this tick (NOT cached, so a later LLM-enabled tick
    can still score them).

    Never raises — on any failure a code resolves to neutral 0.0.
    """
    cfg = cfg or SentimentConfig()
    as_of = as_of or date.today()
    as_of_s = as_of.isoformat()
    fetch = news_fetcher or fetch_recent_news

    # Dedupe preserving order, then cap to bound cost.
    ordered = list(dict.fromkeys(codes))[: cfg.max_codes]

    scores: dict[str, float] = {}
    to_score: list[dict] = []

    for code in ordered:
        cached = db.get_news_sentiment(code, as_of_s)
        if cached is not None:
            scores[code] = float(cached["score"])
            continue
        try:
            news = fetch(code, limit=cfg.max_news_per_code,
                         lookback_days=cfg.lookback_days, as_of=as_of)
        except Exception as e:
            logger.debug("news fetch failed for %s: %s", code, e)
            news = []
        headlines = [n.get("title", "") for n in news if n.get("title")]
        if not headlines:
            # Cache neutral so we don't keep hammering the news endpoint today.
            db.upsert_news_sentiment(code, as_of_s, 0.0, reason="无近期新闻",
                                     n_news=0, model=cfg.model)
            scores[code] = 0.0
            continue
        to_score.append({"code": code, "headlines": headlines})

    if to_score and llm_client is not None:
        try:
            llm_scores = _score_with_llm(llm_client, to_score, cfg)
        except Exception as e:
            logger.warning("sentiment LLM scoring failed: %s", e)
            llm_scores = {}
        for item in to_score:
            code = item["code"]
            score, reason = llm_scores.get(code, (0.0, ""))
            db.upsert_news_sentiment(code, as_of_s, score, reason=reason,
                                     n_news=len(item["headlines"]),
                                     model=cfg.model)
            scores[code] = score
    else:
        # No LLM this tick — neutral, and intentionally NOT cached.
        for item in to_score:
            scores[item["code"]] = 0.0

    return scores
