"""非有限浮点(NaN/±Inf)的 JSON 净化 —— 全项目共用一份实现。

**为什么需要它**:裸 `NaN`/`Infinity` 不是合法 JSON,但 Python 的
`json.dumps` 默认 `allow_nan=True` 照写不误,`json.loads` 默认又能把它读回
`float('nan')` —— 于是本地读写一路「看着正常」,直到 starlette 的
`JSONResponse` 用 `allow_nan=False` 序列化响应才炸成 500。前端的
`JSON.parse` 同样吃不下裸 NaN。

这个坑在本项目已经从**两个不同出口**冒出来过:

  * `/api/regime/*` —— breadth 对空切片求 mean/median 得 NaN,经
    `regime/report.py` 落库(#153);
  * `/api/agent/decisions` —— 同一批 NaN 指标又被 `log_decision` 塞进
    `agent_decisions.details_json`。

所以修的位置不是某个模块,而是**两条边界**:
  1. 持久化边界(`Database.log_decision`、`regime.report.save`)—— 不让非法
     JSON 进库;
  2. 响应边界(`api.app.SafeJSONResponse`)—— 兜住任何已落库的旧数据和未来
     任何新的 NaN 来源,任一端点都不会再因此 500。

语义上「算不出来」= JSON `null`,不是 0(填 0 会被读成「真的是 0」)。
"""

from __future__ import annotations

import math


def json_safe(obj):
    """递归把非有限浮点(NaN/±Inf)换成 None,其余原样返回。

    只在容器/浮点上递归,其它类型原样透传 —— 这是净化器不是序列化器,
    不认识的对象留给 `json.dumps` 自己去报错,免得把真正的类型问题吞掉。
    """
    if isinstance(obj, float):        # np.float64 是 float 子类,一并覆盖
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj
