from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import tools
from .client import EdgarClient, EdgarError

srv = MCPServer(
    name="edgar",
    version="0.1.0",
    instructions=(
        "Query SEC EDGAR: company filings, filing text, and XBRL financial facts. "
        "Resolve a company first, then list filings or pull concepts. Filing text is "
        "windowed — follow next_offset to keep reading."
    ),
)

_client: EdgarClient | None = None


def client() -> EdgarClient:
    global _client
    if _client is None:
        # Path("") is PosixPath(".") and is truthy, so an unset EDGAR_CACHE would
        # otherwise scatter the cache across whatever the cwd happens to be.
        override = os.environ.get("EDGAR_CACHE") or None
        _client = EdgarClient(cache_dir=Path(override) if override else None)
    return _client


def _err(e: Exception) -> dict:
    return {"error": str(e)}


@srv.tool(description="Resolve a ticker, CIK, or company name to its EDGAR identity.")
async def lookup_company(query: str) -> dict:
    try:
        return await tools.resolve_company(client(), query)
    except EdgarError as e:
        return _err(e)


@srv.tool(
    description="List a company's filings, newest first. Filter by form type "
    "(10-K, 8-K, DEF 14A, 4, ...) and filing-date range (YYYY-MM-DD)."
)
async def list_filings(
    query: str,
    forms: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 25,
) -> dict:
    try:
        return await tools.list_filings(client(), query, forms, since, until, limit)
    except EdgarError as e:
        return _err(e)


@srv.tool(
    description="Fetch the text of a filing document from its EDGAR Archives URL. "
    "Returns a window; if truncated is true, call again with next_offset."
)
async def get_filing_text(url: str, offset: int = 0, limit: int = 40000) -> dict:
    try:
        return await tools.get_filing_text(client(), url, offset, limit)
    except EdgarError as e:
        return _err(e)


@srv.tool(
    description="Time series for one XBRL concept (e.g. Revenues, Assets, "
    "NetIncomeLoss) as reported by a company across filings."
)
async def get_concept(
    query: str, tag: str, taxonomy: str = "us-gaap", limit: int = 20
) -> dict:
    try:
        return await tools.get_concept(client(), query, tag, taxonomy, limit)
    except EdgarError as e:
        return _err(e)


@srv.tool(
    description="List the XBRL tags a company actually reports, most-reported first. "
    "Use this to find the right tag before calling get_concept."
)
async def list_concepts(query: str, contains: str | None = None) -> dict:
    try:
        return await tools.list_concepts(client(), query, contains)
    except EdgarError as e:
        return _err(e)


@srv.tool(
    description="Full-text search across EDGAR filings from 2001 onward. "
    "Returns matching filings with document URLs."
)
async def search_filings(
    q: str,
    forms: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
) -> dict:
    try:
        return await tools.search_filings(client(), q, forms, since, until, limit)
    except EdgarError as e:
        return _err(e)


@srv.tool(
    description="Compare one XBRL concept across all filers for a period, ranked by "
    "value. Period is CY2023 (annual), CY2023Q1 (duration), or CY2023Q1I (instant)."
)
async def compare_concept(
    tag: str, period: str, unit: str = "USD", taxonomy: str = "us-gaap", limit: int = 25
) -> dict:
    try:
        return await tools.compare_concept(client(), tag, period, unit, taxonomy, limit)
    except EdgarError as e:
        return _err(e)


@srv.tool(description="Cache hit rate, request count, and bytes downloaded this session.")
async def cache_stats() -> dict:
    return client().stats.as_dict()


def main() -> None:
    srv.run("stdio")


if __name__ == "__main__":
    main()
