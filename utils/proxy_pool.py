"""
utils/proxy_pool.py
~~~~~~~~~~~~~~~~~~~
Async proxy pool that fetches free proxies from the GeoNode API and rotates
through them for Cardmarket requests.

Usage
-----
    from utils.proxy_pool import proxy_pool

    proxy_url = await proxy_pool.get()   # e.g. "http://1.2.3.4:8080"
    if proxy_url is None:
        # pool is empty / disabled – fall back to direct connection
        ...

The pool is lazy-loaded: proxies are fetched on the first call to ``get()``
and refreshed automatically after ``refresh_hours`` hours.

Configuration
-------------
Set ``GEONODE_PROXY_URL`` in your ``.env`` (or environment) to point at any
GeoNode-compatible proxy-list API.  The default targets Dutch fast proxies:

    GEONODE_PROXY_URL=https://proxylist.geonode.com/api/proxy-list?country=NL&speed=fast&page=1&limit=500&sort_by=responseTime&sort_type=desc

Set ``PROXY_POOL_REFRESH_HOURS`` to control how often the list is refreshed
(default: 1 hour).  Set to 0 to disable automatic refresh.

Set ``PROXY_POOL_ENABLED=false`` to disable the pool entirely (the pool
will always return ``None`` so callers fall through to a direct connection).
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)

_GEONODE_DEFAULT_URL = (
    "https://proxylist.geonode.com/api/proxy-list"
    "?country=NL&speed=fast&page=1&limit=500"
    "&sort_by=responseTime&sort_type=desc"
)


class ProxyPool:
    """Async proxy pool backed by a GeoNode-compatible API.

    Parameters
    ----------
    api_url:
        Full URL of the GeoNode proxy-list endpoint.
    refresh_hours:
        How often (in hours) to re-fetch the list.  0 means never refresh
        after the initial load.
    enabled:
        When ``False`` the pool is disabled and ``get()`` always returns
        ``None``.
    """

    def __init__(
        self,
        api_url: str = _GEONODE_DEFAULT_URL,
        refresh_hours: float = 1.0,
        enabled: bool = True,
    ) -> None:
        self._api_url = api_url
        self._refresh_seconds = refresh_hours * 3600
        self._enabled = enabled

        self._proxies: list[str] = []
        self._index: int = 0
        self._last_fetched: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self) -> str | None:
        """Return the next proxy URL from the pool, or ``None`` when unavailable.

        The pool is automatically refreshed when it is empty or stale.
        """
        if not self._enabled:
            return None

        await self._refresh_if_needed()

        if not self._proxies:
            logger.warning("ProxyPool: no proxies available – using direct connection")
            return None

        proxy_url = self._proxies[self._index % len(self._proxies)]
        self._index += 1
        return proxy_url

    async def get_random(self) -> str | None:
        """Return a random proxy URL, or ``None`` when unavailable."""
        if not self._enabled:
            return None

        await self._refresh_if_needed()

        if not self._proxies:
            return None

        return random.choice(self._proxies)

    async def refresh(self) -> None:
        """Force-refresh the proxy list from the API."""
        async with self._lock:
            await self._fetch()

    @property
    def size(self) -> int:
        """Number of proxies currently in the pool."""
        return len(self._proxies)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _refresh_if_needed(self) -> None:
        """Refresh the pool if it has never been loaded or has gone stale."""
        now = time.monotonic()
        needs_refresh = (
            not self._proxies
            or (
                self._refresh_seconds > 0
                and (now - self._last_fetched) >= self._refresh_seconds
            )
        )
        if not needs_refresh:
            return

        async with self._lock:
            # Re-check inside the lock to avoid a double-refresh race.
            now = time.monotonic()
            needs_refresh = (
                not self._proxies
                or (
                    self._refresh_seconds > 0
                    and (now - self._last_fetched) >= self._refresh_seconds
                )
            )
            if needs_refresh:
                await self._fetch()

    async def _fetch(self) -> None:
        """Fetch and parse the proxy list from the GeoNode API."""
        logger.info("ProxyPool: fetching proxy list from %s", self._api_url)
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._api_url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "ProxyPool: API returned HTTP %d – keeping old list",
                            resp.status,
                        )
                        return
                    data: dict[str, Any] = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ProxyPool: failed to fetch proxy list: %s", exc)
            return

        entries: list[dict[str, Any]] = data.get("data", [])
        proxies: list[str] = []
        for entry in entries:
            ip = entry.get("ip", "").strip()
            port = str(entry.get("port", "")).strip()
            protocols: list[str] = entry.get("protocols", [])
            if not ip or not port:
                continue
            scheme = "http"
            if "socks5" in protocols:
                scheme = "socks5"
            elif "socks4" in protocols:
                scheme = "socks4"
            elif "https" in protocols:
                scheme = "http"
            proxies.append(f"{scheme}://{ip}:{port}")

        if proxies:
            random.shuffle(proxies)
            self._proxies = proxies
            self._index = 0
            self._last_fetched = time.monotonic()
            logger.info("ProxyPool: loaded %d proxies", len(proxies))
        else:
            logger.warning("ProxyPool: API returned 0 usable proxies")


def _build_proxy_pool() -> ProxyPool:
    """Build the singleton ProxyPool from environment / settings."""
    import os  # noqa: PLC0415

    enabled_str = os.getenv("PROXY_POOL_ENABLED", "").lower()
    enabled = enabled_str not in ("0", "false", "no")

    api_url = os.getenv("GEONODE_PROXY_URL", _GEONODE_DEFAULT_URL)
    refresh_hours = float(os.getenv("PROXY_POOL_REFRESH_HOURS", "1"))

    return ProxyPool(api_url=api_url, refresh_hours=refresh_hours, enabled=enabled)


# Singleton – import this everywhere.
proxy_pool: ProxyPool = _build_proxy_pool()
