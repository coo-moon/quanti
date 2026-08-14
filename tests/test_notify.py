"""告警推送通道:payload 适配 / 白名单 / 去抖 / log_decision 挂钩 / API 配置。"""

from __future__ import annotations

from datetime import date

import pytest

from quanti import notify
from quanti.data.database import Database


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """每个用例独立:重置单例 worker(去抖状态)+ 清 env。"""
    monkeypatch.setattr(notify, "_worker", notify._AlertWorker())
    monkeypatch.delenv("QUANTI_ALERT_WEBHOOK", raising=False)
    monkeypatch.delenv("QUANTI_ALERT_KINDS", raising=False)
    monkeypatch.delenv("QUANTI_ALERT_DEDUPE_SEC", raising=False)


@pytest.fixture
def sent(monkeypatch):
    """截获出站推送(不真发 HTTP)。"""
    calls: list[tuple[str, str]] = []

    def fake_post(url: str, text: str):
        calls.append((url, text))
        return True, "ok"

    monkeypatch.setattr(notify, "_post", fake_post)
    return calls


# ---------------------------------------------------------------- payload 适配
def test_payload_feishu():
    payload, form = notify._build_request(
        "https://open.feishu.cn/open-apis/bot/v2/hook/xxx", "hi")
    assert payload == {"msg_type": "text", "content": {"text": "hi"}}
    assert form is None


def test_payload_dingtalk_and_wecom():
    for url in ("https://oapi.dingtalk.com/robot/send?access_token=x",
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"):
        payload, form = notify._build_request(url, "hi")
        assert payload == {"msgtype": "text", "text": {"content": "hi"}}
        assert form is None


def test_payload_serverchan_is_form():
    payload, form = notify._build_request(
        "https://sctapi.ftqq.com/SCTxxx.send", "标题行\n正文")
    assert payload is None
    assert form["title"] == "标题行"
    assert "正文" in form["desp"]


def test_payload_generic_json():
    payload, form = notify._build_request("https://example.com/hook", "hi")
    assert payload == {"text": "hi"}
    assert form is None


def test_format_text_has_account_kind_and_truncates(monkeypatch):
    monkeypatch.setenv("QUANTI_ACCOUNT", "live")
    text = notify._format_text("portfolio_stop", "熔断", "000001",
                               {"blob": "x" * 5000})
    assert text.startswith("[quanti·live] portfolio_stop")
    assert "熔断" in text and "000001" in text
    assert len(text) <= notify._TEXT_LIMIT


# ---------------------------------------------------------------- 过滤与去抖
def test_not_whitelisted_kind_skipped(sent, monkeypatch):
    monkeypatch.setenv("QUANTI_ALERT_WEBHOOK", "https://example.com/hook")
    assert notify.notify_decision("llm_cycle", "普通决策") is False
    notify.flush()
    assert sent == []


def test_no_webhook_means_disabled(sent):
    assert notify.notify_decision("portfolio_stop", "熔断") is False
    notify.flush()
    assert sent == []


def test_whitelisted_kind_pushed(sent, monkeypatch):
    monkeypatch.setenv("QUANTI_ALERT_WEBHOOK", "https://example.com/hook")
    assert notify.notify_decision("portfolio_stop", "熔断", code="000001") is True
    notify.flush()
    assert len(sent) == 1
    assert sent[0][0] == "https://example.com/hook"
    assert "portfolio_stop" in sent[0][1]


def test_dedupe_same_kind_code(sent, monkeypatch):
    monkeypatch.setenv("QUANTI_ALERT_WEBHOOK", "https://example.com/hook")
    assert notify.notify_decision("portfolio_stop", "一") is True
    assert notify.notify_decision("portfolio_stop", "二") is False  # 窗口内去抖
    assert notify.notify_decision("doctor_warn", "另一类") is True  # 不同 kind 不受影响
    notify.flush()
    assert len(sent) == 2


def test_kinds_env_override(sent, monkeypatch):
    monkeypatch.setenv("QUANTI_ALERT_WEBHOOK", "https://example.com/hook")
    monkeypatch.setenv("QUANTI_ALERT_KINDS", "my_kind")
    assert notify.notify_decision("portfolio_stop", "默认白名单被覆盖") is False
    assert notify.notify_decision("my_kind", "自定义") is True
    notify.flush()
    assert len(sent) == 1


def test_env_wins_over_db_url(monkeypatch):
    monkeypatch.setenv("QUANTI_ALERT_WEBHOOK", "https://env.example/hook")
    assert notify.resolve_webhook("https://db.example/hook") == "https://env.example/hook"
    monkeypatch.delenv("QUANTI_ALERT_WEBHOOK")
    assert notify.resolve_webhook("https://db.example/hook") == "https://db.example/hook"
    assert notify.resolve_webhook("") == ""


def test_send_test_requires_url():
    ok, msg = notify.send_test("")
    assert ok is False and "未配置" in msg


def test_post_failure_never_raises(monkeypatch):
    monkeypatch.setenv("QUANTI_ALERT_WEBHOOK", "https://example.com/hook")

    def boom(url, text):
        raise RuntimeError("network on fire")

    # _post 内部兜异常;这里直接打穿到 worker 也不许外溢
    monkeypatch.setattr(notify, "_post", lambda u, t: (False, "HTTP 500"))
    assert notify.notify_decision("portfolio_stop", "熔断") is True
    notify.flush()  # worker 消化失败不崩


# ---------------------------------------------------------------- DB 挂钩
@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


def test_log_decision_pushes_whitelisted(db, sent, monkeypatch):
    monkeypatch.setenv("QUANTI_ALERT_WEBHOOK", "https://example.com/hook")
    db.log_decision("portfolio_stop", "组合熔断:已清仓", code="000001",
                    details={"dd": -0.31})
    notify.flush()
    assert len(sent) == 1
    assert "组合熔断" in sent[0][1]


def test_log_decision_ordinary_kind_not_pushed(db, sent, monkeypatch):
    monkeypatch.setenv("QUANTI_ALERT_WEBHOOK", "https://example.com/hook")
    db.log_decision("llm_cycle", "常规决策")
    notify.flush()
    assert sent == []


def test_log_decision_uses_db_webhook(db, sent):
    db.set_alert_webhook("https://db.example/hook")
    db.log_decision("doctor_warn", "体检发现问题")
    notify.flush()
    assert len(sent) == 1
    assert sent[0][0] == "https://db.example/hook"


def test_alert_webhook_roundtrip_and_upsert_preserves(db):
    assert db.get_alert_webhook() == ""
    db.set_alert_webhook("https://db.example/hook")
    assert db.get_alert_webhook() == "https://db.example/hook"
    # 数据源配置更新不得抹掉 webhook
    db.upsert_app_config("akshare", "tok")
    assert db.get_alert_webhook() == "https://db.example/hook"
    assert db.get_app_config()["alert_webhook_url"] == "https://db.example/hook"
    db.set_alert_webhook("")
    assert db.get_alert_webhook() == ""


def test_notify_crash_does_not_break_log_decision(db, monkeypatch):
    monkeypatch.setenv("QUANTI_ALERT_WEBHOOK", "https://example.com/hook")
    monkeypatch.setattr(notify, "notify_decision",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    rid = db.log_decision("portfolio_stop", "熔断")  # 不许抛
    assert rid > 0


# ---------------------------------------------------------------- 实盘双闸提醒
def test_live_caps_unset_logged(tmp_path, sent, monkeypatch):
    from quanti.data.provider import DataProvider
    from quanti.execution.qmt_broker import QmtBroker

    monkeypatch.delenv("QUANTI_MAX_ORDER_NOTIONAL", raising=False)
    monkeypatch.delenv("QUANTI_MAX_LIVE_EXPOSURE", raising=False)
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    d.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    provider = DataProvider(d)

    class DeadBridge:
        def get(self, path, params=None):
            raise ConnectionError("down")

        def post(self, path, json=None):
            raise ConnectionError("down")

    QmtBroker(d, provider, client=DeadBridge(), require_live=True,
              session_fn=lambda: True)
    assert d.list_decisions(kind="live_caps_unset")

    # 配了任一闸就不再提醒
    d2 = Database(str(tmp_path / "t2.db"))
    d2.initialize()
    QmtBroker(d2, DataProvider(d2), client=DeadBridge(), require_live=True,
              session_fn=lambda: True, max_order_notional=50_000)
    assert d2.list_decisions(kind="live_caps_unset") == []
    d.close()
    d2.close()
