"""Tushare(sina 爬虫)实时行情 —— 腾讯源的兜底。

`ts.realtime_quote(src="sina")` 是 tushare SDK 内置的新浪爬虫(与 pro 积分
接口无关,仅需 set_token 过 SDK 门槛;2026-08-20 本机实测:批量 5 只 292ms,
带 DATE/TIME 戳)。与 qt.gtimg.cn 是两条独立通道、独立限频,腾讯退避时它
大概率还活着——作为 `quanti.data.realtime` 组合器的第二源。

与 tencent_quotes 同一契约:
- 返回 {code: raw_last_price},只含**今天(北京时间)打过成交戳**的票——
  停牌/退市票直接缺席,mark 落回日线收盘、卖单排队,绝不拿旧价成交;
- 单槽 TTL 缓存 + 失败退避,守护 5s 轮询与 UI 并发拉取不会打爆源。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_BEIJING = timezone(timedelta(hours=8))
_BATCH = 50          # sina 单请求码数上限(保守值)
_TTL_SEC = 15.0      # 同 tencent_quotes:UI 轮询节奏内共享一次拉取
_FAIL_BACKOFF_SEC = 60.0

_cache: tuple[float, frozenset[str], dict[str, float]] | None = None
_last_fail: float | None = None


def _ts_symbol(code: str) -> str:
    """6 位裸代码 → tushare 后缀符号(.SH/.SZ/.BJ)。"""
    if code[:2] in ("43", "83", "87", "92"):
        return code + ".BJ"
    return code + (".SH" if code[0] in "69" else ".SZ")


def _rows_to_prices(rows: list[dict], require_date: str) -> dict[str, float]:
    """realtime_quote 的行 → {裸code: last};只收今天打印且价>0 的行。

    纯函数,rows 取 df.to_dict("records")(键含 TS_CODE/PRICE/DATE)。
    """
    out: dict[str, float] = {}
    for r in rows:
        try:
            code = str(r.get("TS_CODE", "")).split(".")[0]
            price = float(r.get("PRICE", 0) or 0)
            d = str(r.get("DATE", "")).strip()
        except (TypeError, ValueError):
            continue
        if code and price > 0 and d == require_date:
            out[code] = price
    return out


def fetch_last_prices(codes: list[str], token: str) -> dict[str, float]:
    """批量拉最新价(raw 轴)。失败返回 {}(记退避),绝不抛。"""
    global _cache, _last_fail
    if not codes or not token:
        return {}
    now = time.monotonic()
    want = frozenset(codes)
    if _cache is not None:
        fetched_at, covered, prices = _cache
        if now - fetched_at < _TTL_SEC and want <= covered:
            return {c: p for c, p in prices.items() if c in want}
    if _last_fail is not None and now - _last_fail < _FAIL_BACKOFF_SEC:
        return {}
    today = datetime.now(_BEIJING).strftime("%Y%m%d")
    try:
        import tushare as ts
        ts.set_token(token)
        prices: dict[str, float] = {}
        batch = sorted(want)
        for i in range(0, len(batch), _BATCH):
            syms = ",".join(_ts_symbol(c) for c in batch[i:i + _BATCH])
            df = ts.realtime_quote(ts_code=syms, src="sina")
            if df is not None and len(df):
                prices.update(_rows_to_prices(df.to_dict("records"), today))
        _cache = (now, want, prices)
        _last_fail = None
        return dict(prices)
    except Exception as e:  # noqa: BLE001 - 行情兜底源绝不外溢
        _last_fail = now
        logger.warning("tushare realtime fetch failed (backoff %ds): %s",
                       int(_FAIL_BACKOFF_SEC), e)
        return {}
