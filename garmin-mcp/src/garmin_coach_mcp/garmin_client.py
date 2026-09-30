"""Thread-safe, lazily authenticated access to python-garminconnect.

Tokens live in ``$GARMINTOKENS/garmin_tokens.json`` (written by the login CLI).
The library refreshes the DI access token on its own and writes rotated tokens
back to the same file (0600, atomic replace). This module only decides when to
(re)load that file and turns library exceptions into messages for the model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import anyio
import requests
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.exceptions import GarminConnectNotFoundError
from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings

log = logging.getLogger("garmin_coach_mcp.garmin")

T = TypeVar("T")

TOKEN_FILE_NAME = "garmin_tokens.json"

NOT_LOGGED_IN = (
    "Garmin Connect is not connected yet. On the server run `scripts/garmin-login.sh` "
    "(one-time interactive login incl. MFA), then retry."
)
SESSION_EXPIRED = (
    "Garmin rejected the stored session (expired or revoked). On the server run "
    "`scripts/garmin-login.sh` to log in again, then retry."
)


class GarminNotLoggedIn(Exception):
    """No usable token file."""


class GarminService:
    """Owns the single Garmin client instance for the process."""

    def __init__(
        self,
        settings: Settings,
        garmin_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._factory = garmin_factory or self._default_factory
        self._lock = threading.RLock()
        self._client: Any | None = None
        self._loaded_mtime: float | None = None
        self._auth_error_mtime: float | None = None
        self._limiter = anyio.CapacityLimiter(max(1, settings.max_concurrent_calls))

    # ------------------------------------------------------------------ files

    @property
    def token_file(self) -> Path:
        return self.settings.tokens_dir / TOKEN_FILE_NAME

    def _token_mtime(self) -> float | None:
        try:
            st = self.token_file.stat()
        except OSError:
            return None
        return st.st_mtime if st.st_size > 0 else None

    def auth_state(self) -> str:
        """'ok' | 'missing' | 'error' from local state only (no network)."""
        mtime = self._token_mtime()
        if mtime is None:
            return "missing"
        if self._auth_error_mtime is not None and self._auth_error_mtime == mtime:
            return "error"
        return "ok"

    # ----------------------------------------------------------------- client

    def _default_factory(self) -> Any:
        garmin = Garmin(retry_attempts=2, retry_max_wait=5.0)
        if self.settings.api_session == "curl_cffi":
            _use_curl_cffi_api_session(garmin)
        return garmin

    def _file_differs_from_client(self) -> bool:
        """True when someone (the login CLI) replaced the tokens on disk."""
        if self._client is None:
            return True
        try:
            on_disk = json.loads(self.token_file.read_text(encoding="utf-8"))
            in_memory = json.loads(self._client.client.dumps())
        except (OSError, ValueError, AttributeError):
            return True
        return on_disk != in_memory

    def get_client(self) -> Any:
        """Return an authenticated client, (re)loading tokens when needed."""
        with self._lock:
            mtime = self._token_mtime()
            if mtime is None:
                self._client = None
                raise GarminNotLoggedIn(NOT_LOGGED_IN)
            if self._client is not None and mtime != self._loaded_mtime:
                if self._file_differs_from_client():
                    log.info("token file changed on disk; reloading Garmin session")
                    self._client = None
                else:
                    self._loaded_mtime = mtime  # our own refresh wrote it
            if self._client is None:
                garmin = self._factory()
                try:
                    garmin.login(tokenstore=str(self.settings.tokens_dir))
                except GarminConnectAuthenticationError:
                    self._auth_error_mtime = mtime
                    raise
                self._client = garmin
                self._loaded_mtime = self._token_mtime()
                self._auth_error_mtime = None
            return self._client

    def _mark_auth_failure(self) -> None:
        with self._lock:
            self._client = None
            self._auth_error_mtime = self._token_mtime()

    # ------------------------------------------------------------------ calls

    def call_sync(self, func: Callable[[Any], T]) -> T:
        """Run ``func(client)`` and translate library errors into ToolError."""
        try:
            client = self.get_client()
            result = func(client)
        except ToolError:
            raise
        except GarminNotLoggedIn as exc:
            raise ToolError(str(exc)) from exc
        except GarminConnectAuthenticationError as exc:
            self._mark_auth_failure()
            raise ToolError(SESSION_EXPIRED) from exc
        except GarminConnectTooManyRequestsError as exc:
            raise ToolError("Garmin Connect rate limit (HTTP 429). Wait a few minutes and retry.") from exc
        except GarminConnectNotFoundError as exc:
            raise ToolError(f"Not found in Garmin Connect ({_short(exc)}).") from exc
        except GarminConnectConnectionError as exc:
            raise ToolError(f"Garmin Connect request failed: {_short(exc)}") from exc
        except (requests.RequestException, OSError) as exc:
            raise ToolError(f"Network error talking to Garmin Connect ({type(exc).__name__}).") from exc
        except ValueError as exc:  # library input validation
            raise ToolError(str(exc)) from exc
        return result

    async def call(self, func: Callable[[Any], T]) -> T:
        return await anyio.to_thread.run_sync(self.call_sync, func, limiter=self._limiter)

    async def call_many(self, funcs: dict[str, Callable[[Any], Any]]) -> dict[str, Any]:
        """Run several independent calls concurrently.

        Authentication problems abort the whole batch (every section would fail
        the same way); any other failure is returned per key as {"error": ...}.
        """
        await self.call(lambda client: None)  # authenticate once up front

        async def one(key: str, fn: Callable[[Any], Any], out: dict[str, Any]) -> None:
            try:
                out[key] = await self.call(fn)
            except ToolError as exc:
                out[key] = _SectionError(str(exc))

        results: dict[str, Any] = {}
        async with anyio.create_task_group() as tg:
            for key, fn in funcs.items():
                tg.start_soon(one, key, fn, results)
        for value in results.values():
            if isinstance(value, _SectionError) and value.message in (NOT_LOGGED_IN, SESSION_EXPIRED):
                raise ToolError(value.message)
        return results

    # -------------------------------------------------------------- keepalive

    async def keepalive_loop(self) -> None:
        """Touch the API periodically so the rotating refresh token stays alive."""
        interval = max(0.25, self.settings.keepalive_hours) * 3600
        await asyncio.sleep(90 + random.uniform(0, 60))
        while True:
            if self._token_mtime() is not None:
                started = time.monotonic()
                try:
                    await self.call(lambda c: c.connectapi("/userprofile-service/socialProfile"))
                    log.info("keepalive ok (%.0f ms)", (time.monotonic() - started) * 1000)
                except ToolError as exc:
                    log.warning("keepalive failed: %s", exc)
                except Exception as exc:  # never let the loop die
                    log.warning("keepalive crashed: %s", type(exc).__name__)
            await asyncio.sleep(interval * random.uniform(0.9, 1.1))


class _SectionError:
    __slots__ = ("message",)

    def __init__(self, message: str) -> None:
        self.message = message


def is_section_error(value: Any) -> bool:
    return isinstance(value, _SectionError)


def section_error(value: Any) -> dict[str, str]:
    return {"error": value.message}


def _short(exc: BaseException, limit: int = 200) -> str:
    text = " ".join(str(exc).split())
    return text[:limit] or type(exc).__name__


def _use_curl_cffi_api_session(garmin: Any) -> None:
    """Workaround for python-garminconnect #444: make API calls look like Chrome."""
    from curl_cffi import requests as cffi_requests

    garmin.client._api_session = cffi_requests.Session(impersonate="chrome")
