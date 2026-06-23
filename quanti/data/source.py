"""Market data-source resolution + adapter factory + connectivity probe.

One switch point for "which historical data source feeds the DB". Realtime
quotes are NOT routed here — they always come from xtdata via the qmt-bridge
(live monitoring). This module only decides the *historical/sync* adapter:

    tushare (default) | akshare | xtdata

Resolution precedence (highest first):
    explicit arg  >  DB app_config.data_source  >  env QUANTI_DATA_SOURCE  >  "tushare"

Token precedence (for tushare): DB app_config.data_source_token > env TUSHARE_TOKEN.

If tushare is selected but unavailable (package not installed or no token),
`make_quote_adapter` falls back to akshare with a warning (so a fresh / no-token
install keeps working) unless `allow_fallback=False` (used by the explicit
backfill so the user isn't silently downgraded).

Kept import-light: the three adapters are imported lazily inside the factory so
importing this module never requires akshare/tushare to be installed.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

VALID_SOURCES = ("tushare", "akshare", "xtdata")
DEFAULT_SOURCE = "tushare"


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
                       allow_fallback: bool = True):
    """Build the daily-quote sync adapter for the resolved source.

    Falls back to akshare (with a warning) when tushare is selected but
    unavailable, unless allow_fallback=False."""
    src = resolve_source(db, source)
    if src == "tushare":
        if tushare_available(db):
            from quanti.data.tushare_adapter import TushareAdapter
            return TushareAdapter(db, token=tushare_token(db))
        msg = ("tushare selected but unavailable (package not installed or no "
               "token configured)")
        if not allow_fallback:
            raise RuntimeError(msg)
        logger.warning("%s; falling back to akshare", msg)
        from quanti.data.akshare_adapter import AkShareAdapter
        return AkShareAdapter(db)
    if src == "xtdata":
        from quanti.data.xtdata_adapter import XtdataAdapter
        return XtdataAdapter(db)
    # akshare (and any unknown value → safe default)
    from quanti.data.akshare_adapter import AkShareAdapter
    return AkShareAdapter(db)


def make_stock_list_adapter(db, source: str | None = None, *,
                            allow_fallback: bool = True):
    """Stock-list (roster) adapter for the resolved source. Same resolution as
    quotes — so the default roster comes from tushare (incl. delisted)."""
    return make_quote_adapter(db, source, allow_fallback=allow_fallback)


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
