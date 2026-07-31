import time

import httpx
import pytest

from edgar_mcp.client import Cache, EdgarClient, EdgarError, RateLimiter

UA = "edgar-mcp-tests tests@example.com"


def mk(handler, tmp_path, **kw):
    return EdgarClient(
        user_agent=UA,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(handler),
        **kw,
    )


async def test_user_agent_required(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    with pytest.raises(EdgarError, match="contact address"):
        EdgarClient(cache_dir=tmp_path)
    # SEC wants a reachable contact, not just any string
    with pytest.raises(EdgarError):
        EdgarClient(user_agent="some-bot", cache_dir=tmp_path)


async def test_rate_limiter_paces_grants():
    rl = RateLimiter(rate=100.0)  # 10ms apart
    t0 = time.monotonic()
    for _ in range(30):
        await rl.acquire()
    assert time.monotonic() - t0 >= 0.25


async def test_rate_limiter_refuses_to_burst():
    """The bug this replaced: a full token bucket let `capacity` requests through
    instantly, so 10/s could put 19 inside one second."""
    rl = RateLimiter(rate=50.0)
    stamps = []
    t0 = time.monotonic()
    await __import__("asyncio").gather(
        *(_stamp(rl, stamps, t0) for _ in range(40))
    )
    worst = max(sum(1 for s in stamps if t <= s < t + 1.0) for t in stamps)
    assert worst <= 50, f"{worst} grants inside one second, limit 50"


async def _stamp(rl, out, t0):
    await rl.acquire()
    out.append(time.monotonic() - t0)


async def test_archives_documents_cached_forever(tmp_path):
    """Filed documents are immutable, so a repeat read must not touch the network."""
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=b"<html>filing</html>")

    url = "https://www.sec.gov/Archives/edgar/data/1/2/x.htm"
    async with mk(handler, tmp_path) as c:
        assert await c.get(url) == b"<html>filing</html>"
        assert await c.get(url) == b"<html>filing</html>"

    assert calls["n"] == 1, "an immutable filing was re-fetched"
    assert c.stats.hits == 1


async def test_data_api_served_from_ttl_without_validators(tmp_path):
    """data.sec.gov sends neither ETag nor Last-Modified, so freshness is a TTL."""
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=b'{"n":1}')  # no validator headers

    async with mk(handler, tmp_path) as c:
        await c.get_json("https://data.sec.gov/x.json")
        await c.get_json("https://data.sec.gov/x.json")
    assert calls["n"] == 1
    assert c.stats.bytes_down == len(b'{"n":1}')


async def test_expired_ttl_refetches(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=b'{"n":1}')

    async with mk(handler, tmp_path, ttl=0.0) as c:
        await c.get_json("https://data.sec.gov/x.json")
        await c.get_json("https://data.sec.gov/x.json")
    assert calls["n"] == 2


async def test_revalidates_with_last_modified_when_stale(tmp_path):
    seen = []

    def handler(req):
        seen.append(req.headers.get("if-modified-since"))
        if req.headers.get("if-modified-since"):
            return httpx.Response(304)
        return httpx.Response(
            200, content=b"hi", headers={"Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"}
        )

    async with mk(handler, tmp_path, ttl=0.0) as c:
        await c.get("https://data.sec.gov/y")
        assert await c.get("https://data.sec.gov/y") == b"hi"
    assert seen[1] == "Wed, 01 Jan 2025 00:00:00 GMT"
    assert c.stats.bytes_down == len(b"hi")  # the 304 carried no body


async def test_retries_then_succeeds(tmp_path):
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, content=b"ok")

    async with mk(handler, tmp_path) as c:
        assert await c.get("https://data.sec.gov/z") == b"ok"
    assert c.stats.retries == 2


async def test_gives_up_and_says_so(tmp_path):
    def handler(req):
        return httpx.Response(429, headers={"Retry-After": "0"})

    async with mk(handler, tmp_path, max_retries=2) as c:
        with pytest.raises(EdgarError, match="429"):
            await c.get("https://data.sec.gov/z")


async def test_404_is_not_retried(tmp_path):
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(404)

    async with mk(handler, tmp_path) as c:
        with pytest.raises(EdgarError, match="not found"):
            await c.get("https://data.sec.gov/nope")
    assert n["i"] == 1


async def test_bad_json_names_the_url(tmp_path):
    async with mk(lambda r: httpx.Response(200, content=b"<html>"), tmp_path) as c:
        with pytest.raises(EdgarError, match="bad JSON"):
            await c.get_json("https://data.sec.gov/broken.json")


async def test_cache_survives_a_torn_meta_file(tmp_path):
    cache = Cache(tmp_path / "c")
    cache.write("https://x/1", b"body", httpx.Headers({"ETag": '"a"'}))
    _, meta = cache._paths("https://x/1")
    meta.write_text("{ not json")
    assert cache.read("https://x/1") == (None, {})
