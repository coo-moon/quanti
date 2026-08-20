"""告警推送通道 — 关键事件经 webhook 主动找到人。

系统此前所有「告警」都是被动的:decision log 要人开 UI 看,logger.warning
进 nohup.out。熔断清仓、实盘未知状态单、bridge 掉线这类事件若无人盯屏就
石沉大海。本模块给 ``Database.log_decision`` 挂一个 kind 白名单钩子:命中
白名单的决策异步 POST 到 webhook(飞书/钉钉/企业微信/Server酱/通用 JSON,
按 URL 自动适配),让自治系统出事时能主动通知人。

设计约束:
- 绝不阻塞、绝不抛错:log_decision 常在锁内/热路径调用,推送走后台守护
  线程 + 有界队列,队列满则丢弃并 logger.warning;任何网络失败只记日志。
- 去抖:同 (kind, code) 在窗口内(默认 600s)只发一条,防告警风暴。
- 配置:env ``QUANTI_ALERT_WEBHOOK`` 优先,其次 app_config 落库值(UI 可配);
  两者皆空 = 通道关闭,零开销。白名单可用 ``QUANTI_ALERT_KINDS``(逗号分隔)
  覆盖;去抖窗口 ``QUANTI_ALERT_DEDUPE_SEC``。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 默认白名单:全部是「不看就可能亏钱/裸奔」的事件。
DEFAULT_ALERT_KINDS = frozenset({
    "portfolio_stop",        # 组合回撤熔断:已清仓
    "cycle_halt",            # 熔断后 agent 停摆
    "kill_switch",           # 人为急停
    "order_submit_unknown",  # 实盘订单状态未知,需人工核对
    "broker_not_live",       # 实盘 bridge 不在线,订单被拒
    "doctor_warn",           # 每日体检发现问题(数据陈旧/离场缺口/库损坏)
    "llm_unavailable",       # LLM 决策层不可用,tick 空过
    "live_caps_unset",       # 实盘敞口双闸(单笔/总敞口)均未配置
    "stale_data_skip",       # 数据大面积陈旧,tick 跳过新信号生成
    "llm_replan_fail",       # 收盘后 LLM 点位重算失败(将退避重试)
})

_MAX_QUEUE = 100
_POST_TIMEOUT = 5.0
_TEXT_LIMIT = 1500  # 各 IM 文本消息普遍 2-4KB 上限,留余量


def alert_kinds() -> frozenset[str]:
    raw = os.environ.get("QUANTI_ALERT_KINDS", "").strip()
    if not raw:
        return DEFAULT_ALERT_KINDS
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


def resolve_webhook(db_url: str | None = None) -> str:
    """env 优先于 DB 落库值;都空返回 ''(通道关闭)。"""
    return os.environ.get("QUANTI_ALERT_WEBHOOK", "").strip() or (db_url or "").strip()


def _format_text(kind: str, summary: str, code: str, details: dict | None) -> str:
    account = os.environ.get("QUANTI_ACCOUNT", "paper")
    lines = [f"[quanti·{account}] {kind}", summary]
    if code:
        lines.append(f"code: {code}")
    if details:
        try:
            import json
            body = json.dumps(details, ensure_ascii=False, default=str)
            if len(body) > 500:
                body = body[:500] + "…"
            lines.append(body)
        except Exception:  # noqa: BLE001 - details 只是补充,坏了就不带
            pass
    return "\n".join(lines)[:_TEXT_LIMIT]


def _build_request(url: str, text: str) -> tuple[dict | None, dict | None]:
    """按 webhook 域名适配消息体 → (json_payload, form_data),二选一非空。"""
    host = urlparse(url).netloc
    if "open.feishu.cn" in host or "open.larksuite.com" in host:
        return {"msg_type": "text", "content": {"text": text}}, None
    if "oapi.dingtalk.com" in host or "qyapi.weixin.qq.com" in host:
        return {"msgtype": "text", "text": {"content": text}}, None
    if "sctapi.ftqq.com" in host:  # Server酱:表单 title/desp
        first, _, rest = text.partition("\n")
        return None, {"title": first[:32] or "quanti alert", "desp": text}
    return {"text": text}, None  # 通用 JSON


def _post(url: str, text: str) -> tuple[bool, str]:
    import httpx
    payload, form = _build_request(url, text)
    try:
        resp = httpx.post(url, json=payload, data=form, timeout=_POST_TIMEOUT)
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        return True, "ok"
    except Exception as e:  # noqa: BLE001 - 告警失败绝不外溢
        return False, str(e)


class _AlertWorker:
    """单例后台推送:有界队列 + 守护线程 + (kind, code) 去抖。"""

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=_MAX_QUEUE)
        self._last_sent: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def _dedupe_window(self) -> float:
        try:
            return float(os.environ.get("QUANTI_ALERT_DEDUPE_SEC", "600"))
        except ValueError:
            return 600.0

    def enqueue(self, url: str, kind: str, code: str, text: str) -> bool:
        key = (kind, code)
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < self._dedupe_window():
                return False
            self._last_sent[key] = now
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="quanti-alert", daemon=True)
                self._thread.start()
        try:
            self._q.put_nowait((url, text))
            return True
        except queue.Full:
            logger.warning("alert queue full, dropped: %s", kind)
            return False

    def _run(self) -> None:
        while True:
            url, text = self._q.get()
            ok, detail = _post(url, text)
            if not ok:
                logger.warning("alert webhook post failed: %s", detail)
            self._q.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """等队列清空(测试用)。守护线程无 join 语义,轮询 unfinished。"""
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)


_worker = _AlertWorker()


def notify_decision(kind: str, summary: str, code: str = "",
                    details: dict | None = None,
                    webhook_url: str | None = None) -> bool:
    """log_decision 钩子:白名单过滤 → 去抖 → 异步推送。永不抛错。

    返回是否真正入队(去抖/关闭/非白名单 → False),仅供测试断言。
    """
    try:
        if kind not in alert_kinds():
            return False
        url = resolve_webhook(webhook_url)
        if not url:
            return False
        text = _format_text(kind, summary, code, details)
        return _worker.enqueue(url, kind, code, text)
    except Exception as e:  # noqa: BLE001 - 告警链路绝不影响决策落库
        logger.warning("notify_decision failed: %s", e)
        return False


def send_test(webhook_url: str) -> tuple[bool, str]:
    """同步发一条测试消息(UI「测试」按钮用),返回 (ok, detail)。"""
    url = (webhook_url or "").strip()
    if not url:
        return False, "webhook 未配置"
    account = os.environ.get("QUANTI_ACCOUNT", "paper")
    return _post(url, f"[quanti·{account}] 测试消息:告警通道连通 ✅")


def flush(timeout: float = 5.0) -> None:
    _worker.flush(timeout)
