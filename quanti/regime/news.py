"""时事/政治面新闻抓取 — 给 regime 报告补上数据面看不见的那一半。

宽度指标能告诉你「市场在做什么」,但不能告诉你「为什么」。政策会议、
海外冲击、行业消息才是解释盘面的那一层,所以 LLM 报告要同时吃到:

* 新闻联播文字稿 (``ak.news_cctv``) — 政治/政策面的官方口径,最权威
* 东财全球财经快讯 (``ak.stock_info_global_em``) — 市场面即时消息

两者都走 akshare(项目已依赖,零新增),且都是**尽力而为**:抓不到就返回
空列表,报告降级成纯数据面,而不是让整个快照失败。新闻是锦上添花,不是
必需品——为它挂掉当日 regime 记录是本末倒置。

akshare 内部用 requests 且不总是设超时,后台守护线程里挂死会拖垮整个
同步 daemon,所以每次抓取都套一层线程超时(``_with_timeout``)。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, timedelta

logger = logging.getLogger(__name__)

#: 单个 akshare 调用的墙钟上限(秒)。超时即放弃该源,不重试——快照每天只跑
#: 一次,少一条新闻远好过卡住 daemon。
FETCH_TIMEOUT = 45.0

#: 喂给 LLM 的条数上限。快讯按时间倒序取最新,联播取当日全部条目。
#: 上限存在的意义是控制 prompt 体积(和 token 成本),不是信息取舍。
MAX_FLASH = 60
MAX_CCTV = 25


def _with_timeout(fn, timeout: float = FETCH_TIMEOUT):
    """跑 fn(),超时/异常都返回 None。调用方负责降级。

    **不要用 `with ThreadPoolExecutor(...)`**:它的 __exit__ 是
    `shutdown(wait=True)`,会在超时之后继续死等那个挂住的 requests 调用 ——
    45 秒的超时形同虚设,整个后台同步 daemon 跟着一起卡住(实测发生过)。
    这里显式 `wait=False` 交回控制权。

    ponytail: 挂住的线程仍会在后台泄漏到它自己的 socket 超时为止 ——
    Python 没法强杀线程。每天只跑一次、最多泄漏一个,可接受;真要根治得让
    akshare 走带 timeout 的 session,那是上游的事。
    """
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fn).result(timeout=timeout)
    except FutureTimeout:
        logger.warning("news fetch timed out after %.0fs (线程后台泄漏,不阻塞)",
                       timeout)
        return None
    except Exception as e:  # noqa: BLE001 - 任一源挂掉都不该炸掉快照
        logger.warning("news fetch failed: %s", e)
        return None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def fetch_cctv(as_of: date) -> list[dict]:
    """新闻联播条目。当日稿件通常在 20:00 后才上线,所以 5 点半跑的快照
    读到的是**昨天**的联播——这正是我们要的:今天开盘前市场消化的就是它。
    往前找最多 4 天,跳过周末/节假日的空档。"""
    import akshare as ak  # noqa: PLC0415 - 懒加载,import akshare 本身就要 1-2s

    for back in range(1, 5):
        d = as_of - timedelta(days=back)
        df = _with_timeout(lambda: ak.news_cctv(date=d.strftime("%Y%m%d")))
        if df is None or getattr(df, "empty", True):
            continue
        cols = set(df.columns)
        title_col = "title" if "title" in cols else ("标题" if "标题" in cols else None)
        content_col = ("content" if "content" in cols
                       else ("内容" if "内容" in cols else None))
        if not title_col:
            continue
        out = []
        for _, row in df.head(MAX_CCTV).iterrows():
            item = {"date": d.isoformat(), "title": str(row[title_col])}
            if content_col:
                # 联播全文动辄数千字,截断到摘要长度——LLM 要的是议题不是全文
                item["summary"] = str(row[content_col])[:300]
            out.append(item)
        if out:
            return out
    return []


def fetch_flash() -> list[dict]:
    """东财全球财经快讯(最新 N 条)。"""
    import akshare as ak  # noqa: PLC0415

    df = _with_timeout(ak.stock_info_global_em)
    if df is None or getattr(df, "empty", True):
        return []
    cols = list(df.columns)
    # akshare 的列名是中文且偶有变动,按位置兜底
    title_col = "标题" if "标题" in cols else cols[0]
    summary_col = "摘要" if "摘要" in cols else (cols[1] if len(cols) > 1 else None)
    time_col = "发布时间" if "发布时间" in cols else None
    out = []
    for _, row in df.head(MAX_FLASH).iterrows():
        item = {"title": str(row[title_col])}
        if summary_col:
            item["summary"] = str(row[summary_col])[:200]
        if time_col:
            item["time"] = str(row[time_col])
        out.append(item)
    return out


def fetch_news(as_of: date | None = None) -> dict:
    """两个源合起来。任一源失败都只是少一块,不抛异常。"""
    as_of = as_of or date.today()
    cctv = fetch_cctv(as_of)
    flash = fetch_flash()
    logger.info("regime news: cctv=%d flash=%d", len(cctv), len(flash))
    return {"cctv": cctv, "flash": flash}


def render_news(news: dict) -> str:
    """新闻 → prompt 片段。空源显式写「未取到」,让 LLM 知道是缺数据
    而不是「今天没新闻」——后者会让它编造一个平静的政策面。"""
    L = []
    cctv = news.get("cctv") or []
    L.append("### 新闻联播(政治/政策面)")
    if cctv:
        L.append(f"({cctv[0]['date']})")
        for it in cctv:
            line = f"- {it['title']}"
            if it.get("summary"):
                line += f" — {it['summary']}"
            L.append(line)
    else:
        L.append("- (未取到,请勿臆测政策面)")
    flash = news.get("flash") or []
    L.append("\n### 财经快讯(市场面,最新在前)")
    if flash:
        for it in flash:
            line = f"- {it['title']}"
            if it.get("summary"):
                line += f" — {it['summary']}"
            L.append(line)
    else:
        L.append("- (未取到,请勿臆测消息面)")
    return "\n".join(L)
