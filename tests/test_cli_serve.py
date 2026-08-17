"""serve/up 端口预检:占用即 fail-fast,不跑 lifespan 副作用。

背景(2026-08-17 实发):uvicorn 先跑 lifespan(启后台同步、按 goal 自动
拉起 agent、写 agent_start 决策)才 bind;launchd KeepAlive 与手动实例
端口冲突时,crash-loop 3 天往 paper.db 刷了 4000+ 条 agent_start。
"""

from __future__ import annotations

import socket

import pytest

from quanti.cli import _preflight_port


def test_preflight_passes_on_free_port():
    # 让 OS 挑一个空闲端口,关掉后立刻预检——应通过
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    _preflight_port("127.0.0.1", port)  # 不抛即过


def test_preflight_exits_when_port_taken():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(SystemExit) as ei:
            _preflight_port("127.0.0.1", port)
        assert "已被占用" in str(ei.value)
    finally:
        holder.close()
