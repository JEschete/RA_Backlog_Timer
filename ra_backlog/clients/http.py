"""Shared retry/backoff for outbound HTTP (item #10).

The old code had none: a 429 from either service produced a silent `None` that
was then cached as though it were a real answer.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, TypeVar

import aiohttp

from ..config import BACKOFF_BASE, BACKOFF_CAP, MAX_RETRIES, log
from ..models import RAAuthError, RATransportError

T = TypeVar("T")

# Status codes worth trying again.
RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})


def backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """Exponential backoff with full jitter, floored by any Retry-After."""
    if retry_after is not None:
        return min(retry_after, BACKOFF_CAP)
    raw = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
    return random.uniform(0, raw)


def _retry_after(resp: aiohttp.ClientResponse) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


async def get_json(session: aiohttp.ClientSession, url: str,
                   params: dict[str, Any] | None = None,
                   *, max_retries: int = MAX_RETRIES) -> Any:
    """GET returning parsed JSON, retrying transient failures.

    Raises RAAuthError on 401/403 and RATransportError once retries are spent,
    so callers can tell "no data" apart from "could not ask".
    """
    last_error: str = "unknown"

    for attempt in range(max_retries + 1):
        try:
            async with session.get(url, params=params) as resp:
                if resp.status in (401, 403):
                    raise RAAuthError(
                        f"Unauthorized ({resp.status}) - check your API key")

                if resp.status in RETRYABLE:
                    last_error = f"HTTP {resp.status}"
                    if attempt < max_retries:
                        delay = backoff_delay(attempt, _retry_after(resp))
                        log.debug("%s from %s; retrying in %.1fs (attempt %d/%d)",
                                  last_error, url, delay, attempt + 1, max_retries)
                        await asyncio.sleep(delay)
                        continue
                    raise RATransportError(f"{last_error} after {max_retries} retries")

                if resp.status != 200:
                    raise RATransportError(f"HTTP {resp.status}")

                return await resp.json(content_type=None)

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                delay = backoff_delay(attempt)
                log.debug("%s on %s; retrying in %.1fs", last_error, url, delay)
                await asyncio.sleep(delay)
                continue
            raise RATransportError(last_error) from exc

    raise RATransportError(last_error)


async def with_retries(fn: Callable[[], Awaitable[T]], *,
                       max_retries: int = MAX_RETRIES,
                       label: str = "call") -> T:
    """Retry an arbitrary coroutine factory. Used for the HLTB library,
    which does its own HTTP and so cannot go through get_json."""
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as exc:
            if attempt >= max_retries:
                raise
            delay = backoff_delay(attempt)
            log.debug("%s failed (%s); retrying in %.1fs", label, exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")
