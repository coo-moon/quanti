"""Tests for the Tencent quote fetcher (parse + symbol mapping, no network)."""

from __future__ import annotations

from quanti.data.tencent_quotes import _parse, _qt_symbol


def test_qt_symbol_prefixes():
    assert _qt_symbol("600519") == "sh600519"   # 沪主板
    assert _qt_symbol("688981") == "sh688981"   # 科创板
    assert _qt_symbol("900901") == "sh900901"   # 沪B
    assert _qt_symbol("000001") == "sz000001"   # 深主板
    assert _qt_symbol("300750") == "sz300750"   # 创业板
    assert _qt_symbol("200011") == "sz200011"   # 深B
    assert _qt_symbol("832000") == "bj832000"   # 北交所
    assert _qt_symbol("920819") == "bj920819"   # 北交所新段


def test_parse_real_payload_shape():
    # Trimmed real qt.gtimg.cn response (2026-07-03), field 3 = last price.
    text = (
        'v_sh600519="1~贵州茅台~600519~1194.45~1203.00~1205.24~34268~14380";\n'
        'v_sz000001="51~平安银行~000001~11.50~11.40~11.60~123456~654321";\n'
        'v_pv_none="";'
    )
    prices = _parse(text)
    assert prices == {"600519": 1194.45, "000001": 11.50}


def test_parse_skips_malformed_price():
    assert _parse('v_sh600000="1~浦发银行~600000~~1.0~2.0";') == {}
