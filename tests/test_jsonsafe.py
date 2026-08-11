"""非有限浮点(NaN/±Inf)的两条防线:落库前净化 + 响应层兜底。

这个坑在本项目从两个不同出口冒出来过 —— /api/regime/*(#153)与
/api/agent/decisions(同一批 NaN 指标经 log_decision 落库)。所以守的是
**边界**,不是某个模块:任一端点、任一数据源都不该再因 NaN 打成 500。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from quanti.api.app import SafeJSONResponse
from quanti.utils.jsonsafe import json_safe


class TestJsonSafe:
    def test_non_finite_becomes_none(self):
        assert json_safe(float("nan")) is None
        assert json_safe(float("inf")) is None
        assert json_safe(float("-inf")) is None

    def test_finite_values_pass_through_unchanged(self):
        assert json_safe(0.0) == 0.0
        assert json_safe(-1.5) == -1.5
        assert json_safe(3) == 3
        assert json_safe("x") == "x"
        assert json_safe(None) is None
        assert json_safe(True) is True

    def test_recurses_into_nested_containers(self):
        got = json_safe({"a": [1.0, float("nan"), {"b": float("inf")}],
                         "c": ("ok", float("-inf"))})
        assert got == {"a": [1.0, None, {"b": None}], "c": ["ok", None]}
        json.dumps(got, allow_nan=False)      # starlette 的口径,不许抛

    def test_numpy_float_is_covered(self):
        """np.float64 是 float 子类;breadth 的空切片统计正是这个类型。"""
        assert json_safe(np.float64("nan")) is None
        assert json_safe(np.float64(1.25)) == 1.25


class TestSafeJSONResponse:
    def test_renders_nan_as_null(self):
        body = SafeJSONResponse(content={"a": float("nan"), "b": 1.5}).body
        assert json.loads(body) == {"a": None, "b": 1.5}

    def test_clean_payload_is_untouched(self):
        body = SafeJSONResponse(content={"a": 1.5, "b": [1, 2]}).body
        assert json.loads(body) == {"a": 1.5, "b": [1, 2]}

    def test_non_nan_valueerror_still_propagates(self):
        """净化只针对 NaN。真正不可序列化的东西必须照常炸出来,
        不能被这层兜底悄悄吞掉。"""
        with pytest.raises((TypeError, ValueError)):
            SafeJSONResponse(content={"bad": object()})
