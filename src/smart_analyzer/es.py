"""最小 Elasticsearch search_after 客户端。"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator, Sequence
from typing import Any

import httpx

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_CAP_S = 30.0


class ElasticClient:
    """按 ``search_after`` 分页读日志，瞬时失败指数退避重试。"""

    def __init__(
        self,
        host: str,
        index: str,
        *,
        page_size: int = 100,
        timeout: float = 60.0,
        retries: int = 6,
        retry_backoff: float = 1.0,
    ) -> None:
        self._url = f"{host.rstrip('/')}/{index}/_search"
        self._page_size = page_size
        self._retries = max(0, retries)
        self._retry_backoff = retry_backoff
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ElasticClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _search(self, body: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = self._client.post(self._url, json=body)
            except httpx.RequestError as exc:
                last_error = exc
            else:
                if resp.status_code < 400:
                    try:
                        payload = resp.json()
                    except ValueError:
                        last_error = RuntimeError(
                            f"ES 返回非 JSON: {(resp.text or '')[:200]}"
                        )
                    else:
                        return payload if isinstance(payload, dict) else {}
                else:
                    last_error = RuntimeError(
                        f"ES 查询失败 {resp.status_code}: {(resp.text or '')[:200]}"
                    )
                    if resp.status_code not in _RETRYABLE_STATUS:
                        raise last_error
            if attempt >= self._retries:
                break
            delay = min(self._retry_backoff * (2**attempt), _RETRY_CAP_S)
            delay *= 0.5 + random.random() * 0.5
            time.sleep(delay)
        assert last_error is not None
        raise last_error

    def scroll(
        self,
        query: dict[str, Any],
        *,
        source: Sequence[str] = ("message", "@timestamp"),
        order: str = "asc",
    ) -> Iterator[dict[str, Any]]:
        """逐条产出命中文档。"""
        search_after: list[Any] | None = None
        while True:
            body: dict[str, Any] = {
                "size": self._page_size,
                "query": query,
                "sort": [
                    {"@timestamp": {"order": order, "unmapped_type": "date"}},
                    {"_doc": {"order": order}},
                ],
                "_source": list(source),
            }
            if search_after is not None:
                body["search_after"] = search_after
            hits = self._search(body).get("hits", {}).get("hits") or []
            if not hits:
                return
            for hit in hits:
                yield hit
            if len(hits) < self._page_size:
                return
            search_after = hits[-1].get("sort")
