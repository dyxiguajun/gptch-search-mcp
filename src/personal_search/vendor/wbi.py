# Adapted from Zijian-Ni/bilibili-mcp-server (MIT), Copyright (c) 2026 Zijian Ni.
# Changes: serialize refresh, validate keys; transport supplied by gateway counts/rates requests.
"""
Bilibili Wbi signing and authentication.

Bilibili uses Wbi (a custom signing mechanism) to authenticate API requests.
This module implements the Wbi parameter signing algorithm as documented at:
https://socialsisteryi.github.io/bilibili-API-collect/docs/misc/sign/wbi.html
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import urllib.parse

import httpx

from personal_search.errors import GatewayError

# Wbi mixin key reordering table (64 entries, first 32 used)
WBI_MIXIN_TABLE: list[int] = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]

# Characters to filter from parameter values (per Bilibili spec)
WBI_FILTER_CHARS: frozenset[str] = frozenset("!'()*")


def _get_mixin_key(img_key: str, sub_key: str) -> str:
    """Generate 32-character mixin key by reordering concatenated img+sub keys."""
    raw = img_key + sub_key
    return "".join(raw[i] for i in WBI_MIXIN_TABLE[:32])


def _filter_value(value: str) -> str:
    """Remove special characters that Bilibili strips from signed values."""
    return "".join(c for c in value if c not in WBI_FILTER_CHARS)


class WbiSigner:
    """Handles Wbi parameter signing for authenticated Bilibili API requests."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._img_key: str = ""
        self._sub_key: str = ""
        self._last_fetch: float = 0.0
        self._cache_duration: float = 3600.0  # 1 hour
        self._lock = asyncio.Lock()

    async def _fetch_keys(self) -> None:
        """Fetch fresh Wbi keys from the nav API."""
        resp = await self._client.get(
            "https://api.bilibili.com/x/web-interface/nav",
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "签名接口没有返回预期 JSON") from None
        if not self.use_nav(data.get("data", {})):
            raise GatewayError("WBI_KEYS_UNAVAILABLE", "平台没有返回有效签名密钥")

    def use_nav(self, data: dict) -> bool:
        """Reuse the already validated auth preflight response, avoiding a second nav GET."""
        wbi_img = data.get("wbi_img", {})

        img_url: str = wbi_img.get("img_url", "")
        sub_url: str = wbi_img.get("sub_url", "")

        # Extract key from URL pattern: .../{key}.png
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0] if img_url else ""
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0] if sub_url else ""
        if not all(
            len(key) == 32 and all(c in "0123456789abcdef" for c in key)
            for key in (img_key, sub_key)
        ):
            return False
        self._img_key, self._sub_key = img_key, sub_key
        self._last_fetch = time.time()
        return True

    async def _ensure_keys(self) -> tuple[str, str]:
        """Return cached keys, refreshing if expired or missing."""
        async with self._lock:
            now = time.time()
            if not self._img_key or (now - self._last_fetch) > self._cache_duration:
                await self._fetch_keys()
        return self._img_key, self._sub_key

    async def sign(self, params: dict[str, str | int]) -> dict[str, str]:
        """
        Sign API parameters with Wbi.

        The algorithm:
          1. Add ``wts`` (current Unix timestamp) to params
          2. Sort params by key, filter special chars from values
          3. URL-encode and append the mixin key
          4. MD5-hash the result → ``w_rid``

        Args:
            params: Original query parameters.

        Returns:
            A new dict containing all original params plus ``w_rid`` and ``wts``.
        """
        img_key, sub_key = await self._ensure_keys()
        mixin_key = _get_mixin_key(img_key, sub_key)

        # Stamp with current time
        signed_params: dict[str, str | int] = {**params, "wts": int(time.time())}

        # Sort, filter, encode
        filtered: dict[str, str] = {
            k: _filter_value(str(v)) for k, v in sorted(signed_params.items())
        }
        query = urllib.parse.urlencode(filtered)
        w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()

        return {**filtered, "w_rid": w_rid}
