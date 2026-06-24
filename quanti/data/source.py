"""Market data-source resolution + adapter factory + connectivity probe.

One switch point for "which historical data source feeds the DB". Realtime
quotes are NOT routed here — they always come from xtdata via the qmt-bridge
(live monitoring). This module only decides the *historical/sync* adapter:

    tushare (default) | akshare | xtdata

Resolution precedence (highest first):
    explicit arg  >  DB app_config.data_source  >  env QUANTI_DATA_SOURCE  >  "tushare"

Token precedence (for tushare): DB app_config.data_source_token > env TUSHARE_TOKEN.

If the selected source is unavailable (e.g. tushare selected but the package is
not installed or no token is configured), `make_quote_adapter` RAISES
`DataSourceUnavailable` — it does NOT silently fall back to akshare, because a
silent vendor swap pollutes the DB with a different-convention/shallower-history
source and breaks "one source per code". To intentionally use akshare, select it
explicitly (UI panel / `--source akshare` / `QUANTI_DATA_SOURCE=akshare`).
Pass `allow_fallback=True` only where best-effort degradation is genuinely wanted.

Call sites that surface errors to the user/UI (API, daemon, agent loop) should
use `try_make_quote_adapter`, which returns `(None, message)` instead of raising.

Kept import-light: the three adapters are imported lazily inside the factory so
importing this module never requires akshare/tushare to be installed.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

VALID_SOURCES = ("tushare", "akshare", "xtdata")
DEFAULT_SOURCE = "tushare"


class DataSourceUnavailable(RuntimeError):
    """Raised when the resolved data source can't be built (e.g. tushare
    selected but not installed / no token). We refuse to silently fall back to
    another vendor — the caller must fix config or pick a source explicitly."""


def resolve_source(db=None, explicit: str | None = None) -> str:
    """explicit > DB app_config > env QUANTI_DATA_SOURCE > default."""
    if explicit:
        return explicit
    if db is not None:
        try:
            cfg = db.get_app_config()
            if cfg.get("data_source"):
                return cfg["data_source"]
        except Exception:  # noqa: BLE001 - missing table / fresh DB → fall through
            pass
    env = os.environ.get("QUANTI_DATA_SOURCE")
    if env:
        return env
    return DEFAULT_SOURCE


def tushare_token(db=None) -> str:
    """DB app_config token > env TUSHARE_TOKEN > ''."""
    if db is not None:
        try:
            tok = db.get_app_config().get("data_source_token")
            if tok:
                return tok
        except Exception:  # noqa: BLE001
            pass
    return os.environ.get("TUSHARE_TOKEN", "") or ""


def _tushare_installed() -> bool:
    from quanti.data import tushare_adapter
    return tushare_adapter.ts is not None


def tushare_available(db=None) -> bool:
    """Usable WITHOUT a network call: package importable AND a token present."""
    return _tushare_installed() and bool(tushare_token(db))


def make_quote_adapter(db, source: str | None = None, *,
                       allow_fallback: bool = False):
    """Build the daily-quote sync adapter for the resolved source.

    Raises `DataSourceUnavailable` when tushare is selected but unavailable
    (no silent vendor swap). Pass allow_fallback=True to degrade to akshare
    with a warning instead — only where best-effort is genuinely wanted."""
    src = resolve_source(db, source)
    if src == "tushare":
        if tushare_available(db):
            from quanti.data.tushare_adapter import TushareAdapter
            return TushareAdapter(db, token=tushare_token(db))
        msg = ("数据源 'tushare' 不可用:未安装 tushare 或未配置 token。"
               "请在前端「数据源」面板填入 token,或设置环境变量 TUSHARE_TOKEN;"
               "若确实要用 akshare,请显式将数据源切到 akshare"
               "(--source akshare / QUANTI_DATA_SOURCE=akshare / 前端面板)。")
        if not allow_fallback:
            raise DataSourceUnavailable(msg)
        logger.warning("%s — allow_fallback 已开,降级到 akshare", msg)
        from quanti.data.akshare_adapter import AkShareAdapter
        return AkShareAdapter(db)
    if src == "xtdata":
        from quanti.data.xtdata_adapter import XtdataAdapter
        return XtdataAdapter(db)
    # akshare (and any unknown value → safe default)
    from quanti.data.akshare_adapter import AkShareAdapter
    return AkShareAdapter(db)


def try_make_quote_adapter(db, source: str | None = None):
    """Non-raising variant for user/UI-facing call sites. Returns
    `(adapter, None)` on success or `(None, message)` when the source is
    unavailable, so the caller can surface a clean error instead of a 500 /
    crash-loop. Unexpected errors are also captured as a message."""
    try:
        return make_quote_adapter(db, source), None
    except DataSourceUnavailable as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001 - any build failure → clean message
        return None, f"数据源初始化失败: {e}"


def make_stock_list_adapter(db, source: str | None = None, *,
                            allow_fallback: bool = False):
    """Stock-list (roster) adapter for the resolved source. Same resolution as
    quotes — so the default roster comes from tushare (incl. delisted). Raises
    DataSourceUnavailable when the source can't be built (no silent fallback)."""
    return make_quote_adapter(db, source, allow_fallback=allow_fallback)


def make_financials_adapter(db, source: str | None = None):
    """Financials adapter following the configured source, exposing
    ``sync_financials_by_period(period)``.

    Unlike quotes, `financials` is its OWN table with no one-source guard (PIT
    via ann_date works regardless of vendor), so here we DO silently fall back to
    free akshare when the configured source can't do financials — the daemon
    must still refresh. Falls back when: tushare is selected but unavailable
    (no token / not installed), the source is xtdata (no financials endpoint),
    or any build error. tushare's path uses the VIP tier (real ann_date); if
    that tier isn't available it logs and returns 0 — but the
    ``*_with_backstop`` helpers below heal that by falling through to akshare.

    The returned adapter exposes BOTH ``sync_financials_by_period(period)`` and
    ``sync_financials(years)`` with uniform semantics across akshare/tushare."""
    adapter, _err = try_make_quote_adapter(db, source)
    if adapter is not None and hasattr(adapter, "sync_financials_by_period"):
        return adapter
    from quanti.data.akshare_adapter import AkShareAdapter
    return AkShareAdapter(db)


def _financials_with_backstop(db, source, call):
    """Run a financials sync via the configured source, falling through to free
    akshare when it returns 0 rows (e.g. tushare selected but no VIP tier) — so
    the financials table always stays fresh. `call(adapter) -> int rows`."""
    from quanti.data.akshare_adapter import AkShareAdapter
    adapter = make_financials_adapter(db, source)
    n = call(adapter)
    if n == 0 and not isinstance(adapter, AkShareAdapter):
        n = call(AkShareAdapter(db))
    return n


def refresh_latest_financials(db, source=None) -> int:
    """Daemon's daily refresh: ONLY the latest report period (cheap), following
    the configured source with an akshare backstop."""
    from quanti.data.akshare_adapter import AkShareAdapter
    period = AkShareAdapter.report_periods(1)[-1]
    return _financials_with_backstop(
        db, source, lambda a: a.sync_financials_by_period(period))


def sync_financials_years(db, years: int, source=None) -> int:
    """UI/CLI backfill: whole-market financials over `years` report periods,
    following the configured source with an akshare backstop."""
    return _financials_with_backstop(db, source, lambda a: a.sync_financials(years))


def probe_source(source: str, token: str | None = None,
                 db=None) -> tuple[bool, str]:
    """Cheap connectivity check for a source. Returns (ok, message). Makes a
    real network call (that's the point) but a tiny one."""
    src = source or DEFAULT_SOURCE
    try:
        if src == "tushare":
            from quanti.data import tushare_adapter
            if tushare_adapter.ts is None:
                return False, "tushare 未安装(pip install 'quanti[data]')"
            tok = token if token is not None else tushare_token(db)
            if not tok:
                return False, "未配置 Tushare token"
            pro = tushare_adapter.ts.pro_api(tok)
            df = pro.trade_cal(exchange="SSE", limit=1)
            if df is None or len(df) == 0:
                return False, "Tushare 返回空(token 可能无效或点数不足)"
            return True, "Tushare 连接成功"
        if src == "akshare":
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            if df is None or len(df) == 0:
                return False, "AkShare 返回空"
            return True, "AkShare 连接成功"
        if src == "xtdata":
            from quanti.bridge_client import DEFAULT_BRIDGE_URL, HttpBridgeClient
            h = HttpBridgeClient(DEFAULT_BRIDGE_URL).get("/health")
            if not h.get("ok"):
                return False, "qmt-bridge 未就绪"
            mode = h.get("mode", "?")
            return True, f"qmt-bridge 连接成功(mode={mode})"
        return False, f"未知数据源: {src}"
    except Exception as e:  # noqa: BLE001 - any failure = not connected
        return False, f"{src} 连接失败: {e}"
