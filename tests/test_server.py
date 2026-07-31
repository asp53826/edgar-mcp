"""End-to-end over the real MCP protocol: schemas are generated from the
signatures, so a bad annotation only shows up here."""

import json

import pytest
from mcp import Client

from edgar_mcp.server import srv

EXPECTED = {
    "lookup_company",
    "list_filings",
    "get_filing_text",
    "get_concept",
    "list_concepts",
    "search_filings",
    "compare_concept",
    "cache_stats",
}


@pytest.fixture
def ua(monkeypatch):
    monkeypatch.setenv("EDGAR_USER_AGENT", "edgar-mcp-tests tests@example.com")


async def test_server_lists_every_tool(ua):
    async with Client(srv) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == EXPECTED


async def test_tools_carry_descriptions_and_schemas(ua):
    async with Client(srv) as c:
        tools = (await c.list_tools()).tools

    for t in tools:
        assert t.description, f"{t.name} has no description for the model to read"
        assert t.input_schema.get("type") == "object"

    by_name = {t.name: t for t in tools}
    props = by_name["list_filings"].input_schema["properties"]
    assert set(props) >= {"query", "forms", "since", "until", "limit"}
    # query is the only thing a caller must supply
    assert by_name["list_filings"].input_schema.get("required") == ["query"]


async def test_errors_come_back_as_data_not_crashes(ua, monkeypatch, tmp_path):
    """A bad ticker should be a readable message the model can recover from,
    not a protocol-level exception."""
    import edgar_mcp.server as s

    monkeypatch.setattr(s, "_client", None)
    async with Client(srv) as c:
        res = await c.call_tool("get_filing_text", {"url": "https://evil.example.com/x"})

    payload = json.loads(res.content[0].text)
    assert not res.is_error
    assert "Archives" in payload["error"]


async def test_cache_stats_roundtrip(ua, monkeypatch):
    import edgar_mcp.server as s

    monkeypatch.setattr(s, "_client", None)
    async with Client(srv) as c:
        res = await c.call_tool("cache_stats", {})
    stats = json.loads(res.content[0].text)
    assert set(stats) >= {"requests", "cache_hits", "hit_rate", "bytes_downloaded"}


async def test_unset_cache_env_does_not_write_to_cwd(ua, monkeypatch, tmp_path):
    """Path("") is PosixPath("."), so a naive `or None` sends the cache to cwd."""
    import edgar_mcp.server as s

    monkeypatch.setattr(s, "_client", None)
    monkeypatch.delenv("EDGAR_CACHE", raising=False)
    monkeypatch.chdir(tmp_path)

    c = s.client()
    assert c.cache.root != tmp_path
    assert not list(tmp_path.glob("*.body"))


async def test_cache_env_is_honoured(ua, monkeypatch, tmp_path):
    import edgar_mcp.server as s

    monkeypatch.setattr(s, "_client", None)
    monkeypatch.setenv("EDGAR_CACHE", str(tmp_path / "custom"))
    assert s.client().cache.root == tmp_path / "custom"
