"""盘中实时价的双源组合:tushare(sina)主源 + 腾讯兜底。

腾讯 qt.gtimg.cn 会间歇性退避失败(2026-08-18/20 实发,单日数百条
warning);基准实测(scripts/bench_realtime_sources.py,2026-08-20)
tushare 的 sina 爬虫延迟同级(p50≈185ms)、尾部更稳、1 req/s 不触限频,
且两源价格逐位一致——用户拍板 tushare 为主源(2026-08-20),腾讯降为
主源整批拉空时的兜底;无 tushare token 时自动退回纯腾讯(行为同 #189
之前)。

只做「整批为空」的兜底,不做逐票补齐:主源对停牌票的缺席是新鲜度过滤
的**正确行为**,逐票去第二源补会把停牌票的旧价捞回来。两源都只回今天
打过成交戳的票,契约一致。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def make_realtime_fetcher(db):
    """返回 (codes) -> {code: raw_last} 的取价函数,给 PaperBroker 当
    realtime_quote_fn。tushare(sina)主源,失败/拉空 → 腾讯兜底;两源
    都空 = 按无实时价处理(排队/落日线)。token 每次调用现读,UI 改配置
    即时生效。"""
    from quanti.data import tencent_quotes, tushare_quotes
    from quanti.data.source import tushare_token

    def fetch(codes: list[str]) -> dict[str, float]:
        if not codes:
            return {}
        token = tushare_token(db)
        if token:
            out = tushare_quotes.fetch_last_prices(codes, token)
            if out:
                return out
        try:
            fallback = tencent_quotes.fetch_last_prices(codes)
        except Exception as e:  # noqa: BLE001 - 兜底源异常按空处理
            logger.warning("tencent realtime fallback raised: %s", e)
            return {}
        if fallback and token:
            logger.info("realtime marks via tencent fallback (%d codes)",
                        len(fallback))
        return fallback

    return fetch
