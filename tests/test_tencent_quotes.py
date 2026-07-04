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


def test_parse_with_require_date_keeps_today_prints_only():
    """Suspended names report their LAST trade (possibly days old) — with
    require_date only same-day prints survive, so a fill can never price
    off a previous day."""
    base = ("1~贵州茅台~600519~1194.45~1203.00~1205.24~34268~14380~19887"
            + "~x" * 22)  # pad so the timestamp lands at field 30
    parts_fresh = base.split("~")
    parts_fresh[30] = "20260703152224"
    parts_stale = list(parts_fresh)
    parts_stale[2], parts_stale[3] = "600000", "8.88"
    parts_stale[30] = "20260630150000"  # suspended since 06-30
    text = ('v_sh600519="' + "~".join(parts_fresh) + '";\n'
            + 'v_sh600000="' + "~".join(parts_stale) + '";')
    assert _parse(text, require_date="20260703") == {"600519": 1194.45}
    # Without the date requirement both parse (display/backtest use).
    assert set(_parse(text)) == {"600519", "600000"}


def test_parse_with_require_date_drops_short_lines():
    """A line without the timestamp field can't prove freshness → dropped."""
    text = 'v_sh600519="1~贵州茅台~600519~1194.45~1203.00";'
    assert _parse(text, require_date="20260703") == {}
    assert _parse(text) == {"600519": 1194.45}
