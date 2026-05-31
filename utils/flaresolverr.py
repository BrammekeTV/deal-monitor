"""
utils/flaresolverr.py
~~~~~~~~~~~~~~~~~~~~~
Async client for FlareSolverr – a proxy server that bypasses Cloudflare
protection by solving JS challenges in a headless browser.

See: https://github.com/FlareSolverr/FlareSolverr

Usage
-----
    from utils.flaresolverr import FlareSolverrClient

    async with FlareSolverrClient() as client:
        html = await client.get("https://www.cardmarket.com/...")

The client falls back gracefully (returns None) when FlareSolverr is not
running, so scrapers can decide whether to retry with a regular httpx
request or skip the page.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from config.settings import settings
from utils.logger import get_logger

logger = get_logger(__name__)

# Default timeout for a single FlareSolverr request (seconds).
# FlareSolverr itself waits up to maxTimeout ms for the challenge to solve.
_DEFAULT_TIMEOUT = 90.0
_DEFAULT_MAX_TIMEOUT_MS = 60_000  # passed to FlareSolverr


class FlareSolverrError(RuntimeError):
    """Raised when FlareSolverr returns a non-ok status."""


class FlareSolverrClient:
    """
    Thin async wrapper around the FlareSolverr REST API.

    Parameters
    ----------
    base_url:
        URL of the running FlareSolverr instance, e.g.
        ``http://localhost:8191``.  Defaults to ``settings.flaresolverr_url``.
    session_id:
        Optional FlareSolverr session name.  When provided the same browser
        context (cookies, fingerprint) is reused across requests which is
        more efficient and less suspicious.  The session is created
        automatically on the first request and destroyed on ``__aexit__``.
    """

    def __init__(
        self,
        base_url: str | None = None,
        session_id: str | None = "deal-monitor",
    ) -> None:
        self._base_url = (base_url or settings.flaresolverr_url).rstrip("/")
        self._session_id = session_id
        self._session_active = False
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "FlareSolverrClient":
        self._client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        if self._session_id:
            await self._create_session()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session_id and self._session_active:
            await self._destroy_session()
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self, url: str, max_timeout_ms: int = _DEFAULT_MAX_TIMEOUT_MS) -> str | None:
        """
        Fetch *url* via FlareSolverr and return the response HTML.

        Returns ``None`` on connection error (FlareSolverr not running)
        so callers can fall back to direct requests.
        """
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": max_timeout_ms,
        }
        if self._session_id and self._session_active:
            payload["session"] = self._session_id

        result = await self._call(payload)
        if result is None:
            return None

        html: str = result.get("response", "")
        logger.debug("FlareSolverr fetched %s (%d chars)", url, len(html))
        return html

    async def is_available(self) -> bool:
        """Return True if FlareSolverr is reachable."""
        client = self._client or httpx.AsyncClient(timeout=5.0)
        try:
            resp = await client.get(f"{self._base_url}/health")
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False
        finally:
            if self._client is None:
                await client.aclose()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _create_session(self) -> None:
        """Create a named browser session in FlareSolverr."""
        payload = {"cmd": "sessions.create", "session": self._session_id}
        result = await self._call(payload)
        if result is not None:
            self._session_active = True
            logger.debug("FlareSolverr session '%s' created", self._session_id)

    async def _destroy_session(self) -> None:
        """Destroy the named browser session to free resources."""
        payload = {"cmd": "sessions.destroy", "session": self._session_id}
        await self._call(payload)
        self._session_active = False
        logger.debug("FlareSolverr session '%s' destroyed", self._session_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _call(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """
        POST *payload* to ``/v1`` and return the ``solution`` dict.

        Returns ``None`` when FlareSolverr is unreachable so callers can
        degrade gracefully instead of crashing.
        """
        assert self._client is not None, "Use FlareSolverrClient as an async context manager"
        endpoint = f"{self._base_url}/v1"
        try:
            resp = await self._client.post(endpoint, json=payload)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        except httpx.ConnectError:
            logger.warning(
                "FlareSolverr is not reachable at %s – skipping", self._base_url
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("FlareSolverr request failed: %s", exc)
            return None

        status: str = data.get("status", "")
        if status != "ok":
            message = data.get("message", "unknown error")
            logger.error("FlareSolverr returned status '%s': %s", status, message)
            return None

        return data.get("solution", {})
