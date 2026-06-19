"""Shared client for talking to the localhost qmt-bridge.

Both the trading side (:class:`quanti.execution.qmt_broker.QmtBroker`) and the
data side (:class:`quanti.data.xtdata_adapter.XtdataAdapter`) speak to the same
bridge process, so the transport lives here in a neutral module rather than in
either layer.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

DEFAULT_BRIDGE_URL = "http://127.0.0.1:18099"


@runtime_checkable
class BridgeClient(Protocol):
    """Minimal transport — lets callers inject an in-process fake (tests) or
    the httpx-backed client (production) interchangeably."""

    def get(self, path: str, params: dict | None = None) -> dict: ...
    def post(self, path: str, json: dict | None = None) -> dict: ...


class HttpBridgeClient:
    """httpx-backed :class:`BridgeClient` pointing at a running qmt-bridge."""

    def __init__(self, base_url: str = DEFAULT_BRIDGE_URL,
                 timeout: float = 10.0) -> None:
        import httpx
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self._client.get(self._base + path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json: dict | None = None) -> dict:
        r = self._client.post(self._base + path, json=json or {})
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()
