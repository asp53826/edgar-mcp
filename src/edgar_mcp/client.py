"""HTTP layer for EDGAR: rate limiting, conditional caching, retries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

# SEC's published ceiling is 10 req/s. We pace under it: a burst that merely
# averages 10 still trips their throttle, and a 403 from EDGAR is sticky.
SEC_RATE_LIMIT = 9.0
USER_AGENT_ENV = "EDGAR_USER_AGENT"

# Filed documents never change once accepted, so they can be cached forever.
# The JSON APIs on data.sec.gov mutate as new filings land.
ARCHIVE_MARKER = "/Archives/"
DEFAULT_TTL = 3600.0


class EdgarError(RuntimeError):
    pass


class RateLimiter:
    """Strict pacer: at most one request every 1/rate seconds.

    A plain token bucket seeded full lets `capacity` requests through instantly
    and then sustains `rate` on top, so a bucket of 10 at 10/s can put 20
    requests inside one second. Models fire tool calls in parallel bursts, which
    is exactly the shape that trips it, so grants are spaced instead.
    """

    def __init__(self, rate: float = SEC_RATE_LIMIT):
        self.rate = rate
        self.interval = 1.0 / rate
        self.next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self.next_slot - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = self.next_slot
            self.next_slot = max(now, self.next_slot) + self.interval


@dataclass
class Stats:
    requests: int = 0
    hits: int = 0          # served from disk after a 304
    misses: int = 0
    bytes_down: int = 0
    retries: int = 0

    def as_dict(self) -> dict:
        total = self.hits + self.misses
        return {
            "requests": self.requests,
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "bytes_downloaded": self.bytes_down,
            "retries": self.retries,
        }


def is_immutable(url: str) -> bool:
    """Archives documents are frozen at acceptance — an accession's file never changes."""
    return ARCHIVE_MARKER in url


class Cache:
    """Disk cache keyed by URL, holding the body plus whatever validators exist.

    Freshness has to be decided per host, because EDGAR is not uniform:

    * ``www.sec.gov/Archives/`` serves ``Last-Modified`` but no ``ETag``. The
      documents are immutable anyway, so a hit needs no network at all.
    * ``data.sec.gov`` (submissions, companyfacts, frames) sends **no**
      validators whatsoever, so a conditional request is impossible and always
      returns a full 200. Those fall back to a TTL.

    Measured before this split: every repeat companyfacts fetch re-downloaded
    3.75 MB while looking like a cache hit.
    """

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self.root / f"{key}.body", self.root / f"{key}.meta"

    def read(self, url: str) -> tuple[bytes | None, dict]:
        body_p, meta_p = self._paths(url)
        if not (body_p.exists() and meta_p.exists()):
            return None, {}
        try:
            return body_p.read_bytes(), json.loads(meta_p.read_text())
        except (OSError, ValueError):
            return None, {}

    def fresh(self, meta: dict, ttl: float) -> bool:
        if meta.get("immutable"):
            return True
        age = time.time() - meta.get("fetched_at", 0)
        return age < ttl

    def write(self, url: str, body: bytes, headers: httpx.Headers) -> None:
        body_p, meta_p = self._paths(url)
        meta = {
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
            "immutable": is_immutable(url),
            "fetched_at": time.time(),
            "url": url,
        }
        # body first; a torn write leaves meta absent and we simply re-fetch
        body_p.write_bytes(body)
        meta_p.write_text(json.dumps(meta))

    def touch(self, url: str) -> None:
        _, meta_p = self._paths(url)
        if not meta_p.exists():
            return
        try:
            meta = json.loads(meta_p.read_text())
            meta["fetched_at"] = time.time()
            meta_p.write_text(json.dumps(meta))
        except (OSError, ValueError):
            os.utime(meta_p, None)


class EdgarClient:
    def __init__(
        self,
        user_agent: str | None = None,
        cache_dir: Path | None = None,
        rate: float = SEC_RATE_LIMIT,
        timeout: float = 30.0,
        max_retries: int = 3,
        ttl: float = DEFAULT_TTL,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        ua = user_agent or os.environ.get(USER_AGENT_ENV)
        if not ua or "@" not in ua:
            raise EdgarError(
                f"SEC requires a User-Agent with a contact address. "
                f"Set {USER_AGENT_ENV}, e.g. "
                f'{USER_AGENT_ENV}="my-project you@example.com"'
            )
        self.user_agent = ua
        self.limiter = RateLimiter(rate)
        self.cache = Cache(cache_dir or Path.home() / ".cache" / "edgar-mcp")
        self.stats = Stats()
        self.max_retries = max_retries
        self.ttl = ttl
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def get(self, url: str, *, use_cache: bool = True, ttl: float | None = None) -> bytes:
        cached, meta = self.cache.read(url) if use_cache else (None, {})

        # An immutable document, or a still-fresh TTL entry, needs no network.
        if cached is not None and self.cache.fresh(meta, self.ttl if ttl is None else ttl):
            self.stats.hits += 1
            return cached

        headers = {}
        if cached is not None:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        delay = 1.0
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire()
            self.stats.requests += 1
            try:
                r = await self._client.get(url, headers=headers)
            except httpx.HTTPError as e:
                if attempt == self.max_retries:
                    raise EdgarError(f"{url}: {e}") from e
                self.stats.retries += 1
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if r.status_code == 304 and cached is not None:
                self.stats.hits += 1
                self.cache.touch(url)
                return cached

            if r.status_code == 200:
                body = r.content
                self.stats.misses += 1
                self.stats.bytes_down += len(body)
                if use_cache:
                    self.cache.write(url, body, r.headers)
                return body

            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == self.max_retries:
                    raise EdgarError(f"{url}: HTTP {r.status_code} after {attempt + 1} tries")
                self.stats.retries += 1
                wait = float(r.headers.get("retry-after", delay))
                await asyncio.sleep(wait)
                delay *= 2
                continue

            if r.status_code == 404:
                raise EdgarError(f"not found: {url}")
            raise EdgarError(f"{url}: HTTP {r.status_code}")

        raise EdgarError(f"{url}: exhausted retries")

    async def get_json(self, url: str, *, use_cache: bool = True, ttl: float | None = None) -> dict:
        raw = await self.get(url, use_cache=use_cache, ttl=ttl)
        try:
            return json.loads(raw)
        except ValueError as e:
            raise EdgarError(f"{url}: bad JSON ({e})") from e
