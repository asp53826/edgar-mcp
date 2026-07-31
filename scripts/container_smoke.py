"""Verify the published container speaks MCP over stdio."""

from __future__ import annotations

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED_TOOLS = {
    "cache_stats",
    "compare_concept",
    "get_concept",
    "get_filing_text",
    "list_concepts",
    "list_filings",
    "lookup_company",
    "search_filings",
}


async def verify() -> None:
    params = StdioServerParameters(
        command="docker",
        args=[
            "run",
            "--rm",
            "-i",
            "-e",
            "EDGAR_USER_AGENT=edgar-mcp-ci ci@example.com",
            "edgar-mcp:ci",
        ],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            result = await session.list_tools()

    actual_tools = {tool.name for tool in result.tools}
    if info.server_info.name != "edgar":
        raise RuntimeError(f"unexpected server name: {info.server_info.name!r}")
    if actual_tools != EXPECTED_TOOLS:
        missing = sorted(EXPECTED_TOOLS - actual_tools)
        extra = sorted(actual_tools - EXPECTED_TOOLS)
        raise RuntimeError(f"tool mismatch: missing={missing}, extra={extra}")

    print(
        f"container smoke test passed: {info.server_info.name} "
        f"{info.server_info.version}; {len(actual_tools)} tools"
    )


asyncio.run(asyncio.wait_for(verify(), timeout=60))
