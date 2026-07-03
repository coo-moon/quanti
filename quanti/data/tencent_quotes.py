"""Tencent realtime quotes (qt.gtimg.cn) — the paper account's in-session
mark source.

Free, no token, batch (~60 codes/request), ~0.15s round-trip. Chosen for the
paper intraday guard after ruling out the alternatives (VERIFIED 2026-07-03
on this machine): eastmoney — akshare's spot/minute endpoints — TLS-
fingerprint-blocks python outright (curl passes, requests fails with any UA,
direct or proxied), and tushare `stk_mins` is rate-limited to 1 call/HOUR on
the 2000-credit tier. Live stays on xtdata via the qmt-bridge; this module is
paper-only by wiring (see execution.factory).
"""

from __future__ import annotations

import time
import urllib.request

_URL = "https://qt.gtimg.cn/q="
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_BATCH = 60      # verified per-request code cap
# Callers sit on the API event loop (snapshot routes) and inside the guard's
# broker lock — keep worst-case stalls short and rare: a fresh fetch happens
# at most once per TTL when healthy, once per backoff window when down.
_TTL_SEC = 15.0       # matches the UI poll cadence so 状态卡 marks stay fresh;
#                       guard tick (60s) always fetches fresh
_TIMEOUT_SEC = 3.0
_FAIL_BACKOFF_SEC = 30.0
# Direct connection, no proxy: the endpoint is domestic and requests through
# the local proxy proved flaky here, while direct is 0.1s (2026-07-03).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# (fetched_at_monotonic, codes_covered, prices) — single-slot TTL cache so a
# minutely guard plus UI status polls don't each hit Tencent. Concurrent
# refresh from guard thread + event loop is a benign race (last write wins).
_cache: tuple[float, frozenset[str], dict[str, float]] | None = None
_last_fail: float | None = None


def _qt_symbol(code: str) -> str:
    """Bare 6-digit A-share code → Tencent symbol (sh/sz/bj prefix)."""
    if code[:2] in ("43", "83", "87", "92"):
        return "bj" + code  # 北交所
    return ("sh" if code[0] in "69" else "sz") + code


def _parse(text: str) -> dict[str, float]:
    """`v_sh600519="1~贵州茅台~600519~1194.45~..."` lines → {code: last}."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split("~")
        if len(parts) > 3:
            try:
                out[parts[2]] = float(parts[3])
            except ValueError:
                continue
    return out


def fetch_last_prices(codes: list[str]) -> dict[str, float]:
    """Batch realtime last prices for bare 6-digit codes.

    Raises on HTTP failure — callers own the fallback (PaperBroker degrades
    to daily-close marks with a warning). After a failure, calls within the
    backoff window fail fast without touching the network, so an outage
    costs one timeout per backoff window instead of one per caller. Unknown
    codes are absent from the result; suspended ones may come back 0.0 —
    callers filter non-positive.
    """
    global _cache, _last_fail
    wanted = frozenset(codes)
    if (_cache and wanted <= _cache[1]
            and time.monotonic() - _cache[0] < _TTL_SEC):
        return {c: p for c, p in _cache[2].items() if c in wanted}
    if (_last_fail is not None
            and time.monotonic() - _last_fail < _FAIL_BACKOFF_SEC):
        raise ConnectionError("qt.gtimg.cn: backing off after recent failure")
    valid = [c for c in codes if len(c) == 6 and c.isdigit()]
    out: dict[str, float] = {}
    try:
        for i in range(0, len(valid), _BATCH):
            batch = valid[i:i + _BATCH]
            req = urllib.request.Request(
                _URL + ",".join(_qt_symbol(c) for c in batch),
                headers={"User-Agent": _UA})
            with _OPENER.open(req, timeout=_TIMEOUT_SEC) as resp:
                out.update(_parse(resp.read().decode("gbk", errors="replace")))
    except Exception:
        _last_fail = time.monotonic()
        raise
    _last_fail = None
    _cache = (time.monotonic(), wanted, out)
    return out
