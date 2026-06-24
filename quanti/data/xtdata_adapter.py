"""xtdata (QMT) data adapter — daily bars sourced via the qmt-bridge.

Mirrors the part of :class:`quanti.data.akshare_adapter.AkShareAdapter` the
background syncer uses (``sync_stock_list`` / ``sync_daily_quotes``), but the
bars come from QMT's ``xtdata`` through the localhost bridge instead of AkShare.

Crucially it writes through the **same** ``db.save_daily_quotes`` exit, so the
``daily_quotes`` table is identical regardless of source — research / backtest /
selection keep reading SQLite unchanged, and only the *sync* step touches QMT
(which is already running during live trading). AkShare then steps back to
fallback + news. See ``docs/plans/2026-06-16-live-trading-qmt.md`` phase ④.

Skeleton status: the bridge's ``/data/*`` endpoints serve a mock in dev (so
this is fully tested here); on the QMT box they return real xtdata — no change
on this side.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from quanti.bridge_client import DEFAULT_BRIDGE_URL, BridgeClient, HttpBridgeClient
from quanti.data.database import Database

logger = logging.getLogger(__name__)


class XtdataAdapter:
    """Fetches A-share data from xtdata (via qmt-bridge) and saves to the DB."""

    def __init__(self, db: Database, *, client: BridgeClient | None = None,
                 bridge_url: str = DEFAULT_BRIDGE_URL) -> None:
        self._db = db
        self._client: BridgeClient = client or HttpBridgeClient(bridge_url)

    def sync_stock_list(self, patient: bool = False) -> int:
        """Fetch + save the A-share list from xtdata. Returns count saved.
        `patient` accepted for adapter-signature parity (xtdata ignores it)."""
        data = self._client.get("/data/stock_list")
        count = 0
        for s in data.get("stocks", []):
            code = str(s.get("code", ""))
            if not code:
                continue
            name = str(s.get("name", code))
            exchange = str(s.get("exchange")
                           or ("SH" if code.startswith("6") else "SZ"))
            try:
                self._db.upsert_stock(code, name, exchange, date(2000, 1, 1), "")
                count += 1
            except Exception as e:  # noqa: BLE001 - one bad row shouldn't abort
                logger.warning("save %s failed: %s", code, e)
        return count

    def sync_daily_quotes(self, code: str, start: date | None = None,
                          end: date | None = None,
                          repair_gaps: bool = True,
                          with_basic: bool = False) -> int:  # parity; ignored
        """Fetch daily bars for ``code`` from xtdata (incremental from the last
        stored bar by default) and save them. Returns rows saved.

        `repair_gaps` is accepted for adapter-signature parity (the syncer/sites
        pass it) but ignored — xtdata is a single source with no cross-source
        gap-fill."""
        if end is None:
            end = date.today()
        if start is None:
            latest = self._db.get_latest_quote_date(code)
            start = latest if latest else date(2020, 1, 1)

        resp = self._client.get("/data/kline", {
            "code": code, "start": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"), "period": "1d"})
        bars = resp.get("bars", [])
        if not bars:
            return 0

        df = pd.DataFrame([{
            "code": code,
            "date": date.fromisoformat(b["date"]),
            "open": float(b["open"]), "high": float(b["high"]),
            "low": float(b["low"]), "close": float(b["close"]),
            # Units assumed canonical (股 / 元) per the bridge contract;
            # # VERIFY xtdata volume unit (股 vs 手) on the QMT box.
            "volume": float(b.get("volume", 0) or 0),
            "amount": float(b.get("amount", 0) or 0),
            "turnover": float(b.get("turnover", 0) or 0),
            "adj_factor": float(b.get("adj_factor", 1.0) or 1.0),
            "source": "xtdata",
        } for b in bars])
        saved = self._db.save_daily_quotes(df)
        logger.info("%s: %d bars [%s~%s] via xtdata", code, saved,
                    df["date"].min(), df["date"].max())
        return saved
