import json

import httpx
import pytest

from edgar_mcp import tools
from edgar_mcp.client import EdgarClient, EdgarError

UA = "edgar-mcp-tests tests@example.com"

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "2": {"cik_str": 1, "ticker": "ACMA", "title": "ACME HOLDINGS"},
    "3": {"cik_str": 2, "ticker": "ACMB", "title": "ACME INDUSTRIES"},
}


@pytest.fixture(autouse=True)
def _clear_ticker_cache():
    tools._ticker_cache = None
    yield
    tools._ticker_cache = None


def routed(routes):
    def handler(req):
        for frag, payload in routes.items():
            if frag in str(req.url):
                if isinstance(payload, (dict, list)):
                    return httpx.Response(200, content=json.dumps(payload).encode())
                return httpx.Response(200, content=payload)
        return httpx.Response(404)

    return handler


def mk(routes, tmp_path):
    return EdgarClient(
        user_agent=UA, cache_dir=tmp_path / "c", transport=httpx.MockTransport(routed(routes))
    )


def test_pad_cik():
    assert tools.pad_cik(320193) == "0000320193"
    assert tools.pad_cik("0000320193") == "0000320193"
    assert tools.pad_cik("CIK320193") == "0000320193"


def test_rows_zips_parallel_arrays():
    block = {
        "accessionNumber": ["a", "b"],
        "filingDate": ["2024-01-01", "2023-01-01"],
        "form": ["10-K", "8-K"],
    }
    out = tools._rows(block)
    assert out == [
        {"accessionNumber": "a", "filingDate": "2024-01-01", "form": "10-K"},
        {"accessionNumber": "b", "filingDate": "2023-01-01", "form": "8-K"},
    ]
    assert tools._rows({}) == []


def test_html_to_text_drops_ixbrl_hidden_facts():
    raw = (
        b"<html><body><div style='display:none'><ix:header><ix:hidden>"
        b"<ix:nonNumeric>false</ix:nonNumeric>2025FY0000320193</ix:hidden></ix:header></div>"
        b"<p>UNITED STATES</p><p>FORM 10-K</p>"
        b"<script>var x=1;</script></body></html>"
    )
    t = tools.html_to_text(raw)
    assert t.startswith("UNITED STATES")
    assert "0000320193" not in t
    assert "var x" not in t


def test_html_to_text_handles_unbalanced_markup():
    # filings are full of unclosed tags; the skip stack must not swallow the rest
    raw = b"<div style='display:none'><span>hidden<div>REAL TEXT</div>"
    assert "hidden" not in tools.html_to_text(raw)


async def test_resolve_by_ticker_and_name(tmp_path):
    async with mk({"company_tickers": TICKERS}, tmp_path) as c:
        assert (await tools.resolve_company(c, "aapl"))["cik"] == 320193
        assert (await tools.resolve_company(c, "MICROSOFT CORP"))["cik"] == 789019


async def test_ambiguous_name_lists_candidates(tmp_path):
    async with mk({"company_tickers": TICKERS}, tmp_path) as c:
        with pytest.raises(EdgarError, match="Did you mean"):
            await tools.resolve_company(c, "ACME")


async def test_exact_ticker_beats_ambiguous_name(tmp_path):
    # "ACMA" is a ticker; it must resolve straight through rather than tripping
    # the ACME-prefix ambiguity check
    async with mk({"company_tickers": TICKERS}, tmp_path) as c:
        assert (await tools.resolve_company(c, "ACMA"))["name"] == "ACME HOLDINGS"


async def test_exact_name_beats_partial(tmp_path):
    async with mk({"company_tickers": TICKERS}, tmp_path) as c:
        assert (await tools.resolve_company(c, "ACME HOLDINGS"))["cik"] == 1


async def test_unknown_company(tmp_path):
    async with mk({"company_tickers": TICKERS}, tmp_path) as c:
        with pytest.raises(EdgarError, match="no company matching"):
            await tools.resolve_company(c, "NOTAREALCO")


async def test_list_filings_walks_overflow_files(tmp_path):
    """Past ~1000 filings EDGAR splits history into extra files. Missing them
    silently truncates history, which is the bug worth having a test for."""
    routes = {
        "company_tickers": TICKERS,
        "submissions/CIK0000320193.json": {
            "name": "Apple Inc.",
            "tickers": ["AAPL"],
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-25-000079"],
                    "filingDate": ["2025-10-31"],
                    "form": ["10-K"],
                    "primaryDocument": ["aapl-20250927.htm"],
                },
                "files": [{"name": "CIK0000320193-submissions-001.json"}],
            },
        },
        "CIK0000320193-submissions-001.json": {
            "accessionNumber": ["0000320193-14-000097"],
            "filingDate": ["2014-10-27"],
            "form": ["10-K"],
            "primaryDocument": ["old.htm"],
        },
    }
    async with mk(routes, tmp_path) as c:
        r = await tools.list_filings(c, "AAPL", forms=["10-K"])

    assert r["matched"] == 2, "older filings from the overflow file were dropped"
    assert [f["filingDate"] for f in r["filings"]] == ["2025-10-31", "2014-10-27"]
    assert r["filings"][0]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019325000079/aapl-20250927.htm"
    )


async def test_filing_text_windows_and_chains(tmp_path):
    body = b"<html><body><p>" + b"A" * 500 + b"</p></body></html>"
    url = "https://www.sec.gov/Archives/edgar/data/1/2/x.htm"
    async with mk({"Archives": body}, tmp_path) as c:
        first = await tools.get_filing_text(c, url, limit=100)
        assert first["returned_chars"] == 100
        assert first["truncated"] and first["next_offset"] == 100

        rest = await tools.get_filing_text(c, url, offset=first["next_offset"], limit=1000)
        assert not rest["truncated"] and rest["next_offset"] is None
        assert first["text"] + rest["text"] == "A" * 500


async def test_filing_text_rejects_offsite_urls(tmp_path):
    async with mk({}, tmp_path) as c:
        with pytest.raises(EdgarError, match="Archives"):
            await tools.get_filing_text(c, "https://evil.example.com/x.htm")


async def test_concept_series_sorted_newest_first(tmp_path):
    routes = {
        "company_tickers": TICKERS,
        "companyconcept": {
            "label": "Net Income (Loss)",
            "units": {
                "USD": [
                    {"val": 100, "end": "2023-12-31", "filed": "2024-02-01", "form": "10-K"},
                    {"val": 200, "end": "2024-12-31", "filed": "2025-02-01", "form": "10-K"},
                ]
            },
        },
    }
    async with mk(routes, tmp_path) as c:
        r = await tools.get_concept(c, "AAPL", "NetIncomeLoss")
    assert [o["value"] for o in r["observations"]] == [200, 100]
    assert r["total_points"] == 2


async def test_compare_concept_ranks_by_value(tmp_path):
    routes = {
        "frames": {
            "label": "Assets",
            "data": [
                {"cik": 1, "entityName": "Small", "val": 10},
                {"cik": 2, "entityName": "Big", "val": 900},
                {"cik": 3, "entityName": "Mid", "val": 50},
            ],
        }
    }
    async with mk(routes, tmp_path) as c:
        r = await tools.compare_concept(c, "Assets", "CY2023Q4I", limit=2)
    assert [x["name"] for x in r["top"]] == ["Big", "Mid"]
    assert r["total_filers"] == 3
