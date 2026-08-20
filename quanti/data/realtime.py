"""盘中实时价的双源组合:腾讯主源 + tushare(sina)兜底。

腾讯 qt.gtimg.cn 会间歇性退避失败(2026-08-18/20 实发,单日数百条
warning),失败窗口内盘中止损/即时成交退化到日线价。tushare 的 sina 爬虫
是独立通道(本机实测可用),腾讯**整批拉空**时用它补一次。

只做「整批为空」的兜底,不做逐票补齐:腾讯对停牌票的缺席是新鲜度过滤的
**正确行为**,逐票去第二源补会把停牌票的旧价捞回来。两源都只回今天打过
成交戳的票,契约一致。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def make_realtime_fetcher(db):
    """返回 (codes) -> {code: raw_last} 的取价函数,给 PaperBroker 当
    realtime_quote_fn。腾讯失败/退避返回空时,若配置了 tushare token 则
    走 sina 兜底;两源都空 = 按无实时价处理(排队/落日线),行为同从前。"""
    from quanti.data import tencent_quotes, tushare_quotes
    from quanti.data.source import tushare_token

    def fetch(codes: list[str]) -> dict[str, float]:
        try:
            out = tencent_quotes.fetch_last_prices(codes)
        except Exception as e:  # noqa: BLE001 - 主源异常按空处理,走兜底
            logger.warning("tencent realtime fetch raised: %s", e)
            out = {}
        if out or not codes:
            return out
        token = tushare_token(db)
        if not token:
            return out
        fallback = tushare_quotes.fetch_last_prices(codes, token)
        if fallback:
            logger.info("realtime marks via tushare fallback (%d codes)",
                        len(fallback))
        return fallback

    return fetch
