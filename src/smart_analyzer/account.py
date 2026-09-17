"""账号公司名查询。"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from smart_analyzer.config import Settings, get_settings


def fetch_company_names(
    account_ids: Sequence[str],
    *,
    settings: Settings | None = None,
    timeout: float = 10.0,
) -> dict[str, str]:
    """查询账户接口，返回 ``account_id -> companyName``。失败跳过。"""
    cfg = settings or get_settings()
    ids = sorted({aid for aid in account_ids if aid and aid not in ("", "-")})
    if not ids or not cfg.account_info_url:
        return {}

    def lookup(account_id: str) -> tuple[str, str]:
        try:
            resp = httpx.get(
                cfg.account_info_url,
                params={
                    "env": cfg.account_env,
                    "accountNo": account_id,
                    "accountName": "",
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            data: Any = resp.json()
        except Exception:
            return account_id, ""
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            return account_id, ""
        name = data.get("companyName")
        if isinstance(name, str) and name.strip():
            return account_id, name.strip()
        return account_id, ""

    names: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
        for account_id, name in pool.map(lookup, ids):
            if name:
                names[account_id] = name
    return names


def account_label(account_id: str, company_names: dict[str, str]) -> str:
    if not account_id or account_id == "-":
        return "-"
    name = company_names.get(account_id, "")
    if name:
        return f"{name} ({account_id})"
    return account_id
