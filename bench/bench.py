"""Measures what this thing actually costs. Hits live EDGAR; be polite about reruns.

    uv run python bench/bench.py
"""

from __future__ import annotations

import asyncio
import shutil
import statistics
import tempfile
import time
from pathlib import Path

import httpx

from edgar_mcp import tools
from edgar_mcp.client import EdgarClient

AAPL_10K = (
    "https://www.sec.gov/Archives/edgar/data/320193/"
    "000032019325000079/aapl-20250927.htm"
)
SUBMISSIONS = "https://data.sec.gov/submissions/CIK0000320193.json"
FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"


def row(label, value):
    print(f"  {label:<44} {value:>18}")


async def timed(fn):
    t0 = time.perf_counter()
    out = await fn()
    return out, (time.perf_counter() - t0) * 1000


async def cache_effectiveness(tmp: Path):
    print("\ncache: cold fetch vs warm read")
    targets = (
        ("submissions  (data.sec.gov, TTL)", SUBMISSIONS),
        ("companyfacts (data.sec.gov, TTL)", FACTS),
        ("10-K document (Archives, immutable)", AAPL_10K),
    )
    async with EdgarClient(cache_dir=tmp / "cold") as c:
        for name, url in targets:
            before = c.stats.bytes_down
            _, cold_ms = await timed(lambda: c.get(url))
            cold_bytes = c.stats.bytes_down - before

            before = c.stats.bytes_down
            _, warm_ms = await timed(lambda: c.get(url))
            warm_bytes = c.stats.bytes_down - before

            row(f"{name} cold", f"{cold_bytes/1e6:.2f} MB / {cold_ms:.0f} ms")
            row(f"{name} warm", f"{warm_bytes/1e6:.2f} MB / {warm_ms:.1f} ms")
            row("speedup", f"{cold_ms/max(warm_ms, 0.001):.0f}x")

    print("\n  validator support (why freshness differs by host)")
    async with httpx.AsyncClient(
        headers={"User-Agent": "edgar-mcp-bench bench@example.com"}, timeout=30
    ) as h:
        for label, url in (("data.sec.gov", SUBMISSIONS), ("www.sec.gov/Archives", AAPL_10K)):
            r = await h.head(url)
            has = [k for k in ("etag", "last-modified") if r.headers.get(k)]
            row(f"  {label}", ", ".join(has) or "none - TTL is the only option")


async def rate_limit_compliance():
    """SEC's ceiling is 10 req/s. Verify we sit under it even when a model fires
    a burst of parallel tool calls."""
    print("\nrate limiter: 40 concurrent requests against a mock transport")
    stamps: list[float] = []

    def handler(req):
        stamps.append(time.monotonic())
        return httpx.Response(200, content=b"{}")

    tmp = Path(tempfile.mkdtemp())
    async with EdgarClient(
        user_agent="bench bench@example.com",
        cache_dir=tmp,
        transport=httpx.MockTransport(handler),
    ) as c:
        t0 = time.perf_counter()
        await asyncio.gather(*(c.get(f"https://data.sec.gov/{i}", use_cache=False) for i in range(40)))
        elapsed = time.perf_counter() - t0

    # worst 1s sliding window
    worst = max(
        sum(1 for s in stamps if t <= s < t + 1.0) for t in stamps
    )
    row("requests", len(stamps))
    row("wall clock", f"{elapsed:.2f} s")
    row("sustained rate", f"{len(stamps)/elapsed:.1f} req/s")
    row("peak requests in any 1s window", f"{worst} (limit 10)")
    shutil.rmtree(tmp, ignore_errors=True)


async def extraction(tmp: Path):
    print("\ntext extraction: Apple FY2025 10-K")
    async with EdgarClient(cache_dir=tmp / "x") as c:
        raw = await c.get(AAPL_10K)

    runs = []
    for _ in range(3):
        t0 = time.perf_counter()
        text = tools.html_to_text(raw)
        runs.append((time.perf_counter() - t0) * 1000)

    naive = _naive_strip(raw)
    row("source document", f"{len(raw)/1e6:.2f} MB")
    row("extracted text", f"{len(text)/1e3:.0f} K chars")
    row("iXBRL scaffolding removed", f"{len(naive)-len(text):,} chars")
    row("extraction time (median of 3)", f"{statistics.median(runs):.0f} ms")
    row("throughput", f"{len(raw)/1e6/(statistics.median(runs)/1000):.1f} MB/s")
    row("windows to read whole filing @40K", f"{-(-len(text)//40_000)}")


def _naive_strip(raw: bytes) -> str:
    """What you get without the display:none / ix:hidden handling."""
    import re
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.p = []

        def handle_data(self, d):
            self.p.append(d)

    p = P()
    p.feed(raw.decode("utf-8", "replace"))
    return re.sub(r"\s+", " ", "".join(p.p)).strip()


async def range_support():
    """Windowing is offset-based, not Range-based. Check whether that's our
    limitation or EDGAR's."""
    print("\nHTTP Range support on Archives (would let windows skip the download)")
    async with httpx.AsyncClient(
        headers={"User-Agent": "edgar-mcp-bench bench@example.com"}, timeout=30
    ) as c:
        r = await c.get(AAPL_10K, headers={"Range": "bytes=0-1023"})
    row("status", r.status_code)
    row("accept-ranges", r.headers.get("accept-ranges", "(absent)"))
    row("bytes returned", f"{len(r.content):,}")
    row("honored", "yes" if r.status_code == 206 else "no - full body sent")


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="edgar-bench-"))
    print("edgar-mcp benchmark")
    try:
        await cache_effectiveness(tmp)
        await rate_limit_compliance()
        await extraction(tmp)
        await range_support()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()


if __name__ == "__main__":
    asyncio.run(main())
