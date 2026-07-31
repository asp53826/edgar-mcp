"""EDGAR domain logic. Everything here returns plain dicts the server hands back as JSON."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from .client import EdgarClient, EdgarError

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"
FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FRAMES = "https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"
FTS = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

_ticker_cache: dict | None = None


def pad_cik(cik: int | str) -> str:
    return str(int(str(cik).lstrip("CIK").lstrip("0") or 0)).zfill(10)


async def _ticker_map(client: EdgarClient) -> dict:
    global _ticker_cache
    if _ticker_cache is None:
        raw = await client.get_json(TICKERS_URL)
        by_ticker, by_name = {}, []
        for row in raw.values():
            t = row["ticker"].upper()
            by_ticker[t] = {"cik": row["cik_str"], "ticker": t, "name": row["title"]}
            by_name.append((row["title"].upper(), t))
        _ticker_cache = {"by_ticker": by_ticker, "by_name": by_name}
    return _ticker_cache


async def resolve_company(client: EdgarClient, query: str) -> dict:
    """Ticker, CIK, or company name -> {cik, ticker, name}."""
    q = query.strip()
    if not q:
        raise EdgarError("empty query")

    # a bare number (or CIK-prefixed one) is a CIK
    if re.fullmatch(r"(CIK)?\d{1,10}", q, re.I):
        cik = pad_cik(q)
        data = await client.get_json(SUBMISSIONS.format(cik=cik))
        tick = data.get("tickers") or []
        return {"cik": int(cik), "ticker": tick[0] if tick else None, "name": data.get("name")}

    m = await _ticker_map(client)
    if q.upper() in m["by_ticker"]:
        return m["by_ticker"][q.upper()]

    needle = q.upper()
    exact = [(n, t) for n, t in m["by_name"] if n == needle]
    partial = [(n, t) for n, t in m["by_name"] if needle in n]
    for pool in (exact, partial):
        if pool:
            if len(pool) > 1 and pool is partial:
                opts = ", ".join(f"{n} ({t})" for n, t in pool[:8])
                raise EdgarError(f"{len(pool)} companies match '{query}'. Did you mean: {opts}")
            return m["by_ticker"][pool[0][1]]
    raise EdgarError(f"no company matching '{query}'")


async def _all_filings(client: EdgarClient, cik: str) -> list[dict]:
    """Flatten filing history, including the overflow files EDGAR splits off past ~1000 filings."""
    data = await client.get_json(SUBMISSIONS.format(cik=cik))
    filings = data.get("filings", {})
    rows = _rows(filings.get("recent", {}))

    for extra in filings.get("files", []):
        name = extra.get("name")
        if not name:
            continue
        older = await client.get_json(f"https://data.sec.gov/submissions/{name}")
        rows.extend(_rows(older))

    return rows


def _rows(block: dict) -> list[dict]:
    """EDGAR ships parallel arrays; zip them into records."""
    keys = ["accessionNumber", "filingDate", "reportDate", "form",
            "primaryDocument", "primaryDocDescription", "size", "items"]
    present = [k for k in keys if k in block]
    if not present:
        return []
    n = len(block[present[0]])
    out = []
    for i in range(n):
        out.append({k: block[k][i] for k in present})
    return out


async def list_filings(
    client: EdgarClient,
    query: str,
    forms: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 25,
) -> dict:
    co = await resolve_company(client, query)
    cik = pad_cik(co["cik"])
    rows = await _all_filings(client, cik)

    if forms:
        want = {f.upper() for f in forms}
        rows = [r for r in rows if r.get("form", "").upper() in want]
    if since:
        rows = [r for r in rows if r.get("filingDate", "") >= since]
    if until:
        rows = [r for r in rows if r.get("filingDate", "") <= until]

    rows.sort(key=lambda r: r.get("filingDate", ""), reverse=True)
    total = len(rows)
    rows = rows[:limit]

    for r in rows:
        acc = r.get("accessionNumber", "").replace("-", "")
        doc = r.get("primaryDocument")
        if acc and doc:
            r["url"] = ARCHIVE.format(cik=int(co["cik"]), accession=acc, doc=doc)

    return {"company": co, "matched": total, "returned": len(rows), "filings": rows}


class _Strip(HTMLParser):
    """HTML -> text, dropping inline-XBRL scaffolding.

    Modern filings are iXBRL: the document opens with a display:none block holding
    every tagged fact, so a naive tag strip yields a few thousand characters of
    'false2025FY0000320193...' before any prose. Anything hidden gets skipped.
    """

    SKIP = {"script", "style", "head", "ix:header", "ix:hidden", "ix:references", "ix:resources"}
    BREAK = {"p", "div", "br", "tr", "table", "li", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping: list[str] = []

    @staticmethod
    def _hidden(tag: str, attrs) -> bool:
        if tag in _Strip.SKIP:
            return True
        for k, v in attrs:
            if k == "style" and v and "display:none" in v.replace(" ", "").lower():
                return True
        return False

    def handle_starttag(self, tag, attrs):
        if self._hidden(tag, attrs):
            self.skipping.append(tag)
            return
        if not self.skipping and tag in self.BREAK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        # self-closing: never opens a scope, so don't touch the skip stack
        if not self.skipping and tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.skipping:
            # unwind to the matching open tag; filings are not always balanced
            while self.skipping and self.skipping.pop() != tag:
                pass

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)

    def text(self) -> str:
        s = "".join(self.parts)
        s = re.sub(r"[ \t\xa0]+", " ", s)
        s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
        return s.strip()


def html_to_text(raw: bytes) -> str:
    p = _Strip()
    p.feed(raw.decode("utf-8", errors="replace"))
    return p.text()


async def get_filing_text(
    client: EdgarClient, url: str, offset: int = 0, limit: int = 40_000
) -> dict:
    """Fetch a filing document as text.

    10-Ks routinely run past a million characters, so this windows rather than
    returning the whole thing — dumping a full filing into a model's context is
    how you burn a session on one document.
    """
    if not re.match(r"https://www\.sec\.gov/Archives/", url):
        raise EdgarError("url must be an https://www.sec.gov/Archives/ document")

    raw = await client.get(url)
    text = html_to_text(raw) if b"<" in raw[:2000] else raw.decode("utf-8", errors="replace")

    total = len(text)
    offset = max(0, min(offset, total))
    window = text[offset : offset + limit]
    end = offset + len(window)
    return {
        "url": url,
        "total_chars": total,
        "offset": offset,
        "returned_chars": len(window),
        "truncated": end < total,
        "next_offset": end if end < total else None,
        "text": window,
    }


async def get_concept(
    client: EdgarClient, query: str, tag: str, taxonomy: str = "us-gaap", limit: int = 20
) -> dict:
    co = await resolve_company(client, query)
    cik = pad_cik(co["cik"])
    data = await client.get_json(CONCEPT.format(cik=cik, taxonomy=taxonomy, tag=tag))

    series = []
    for unit, points in data.get("units", {}).items():
        for p in points:
            series.append({
                "unit": unit,
                "value": p.get("val"),
                "start": p.get("start"),
                "end": p.get("end"),
                "fy": p.get("fy"),
                "fp": p.get("fp"),
                "form": p.get("form"),
                "filed": p.get("filed"),
                "frame": p.get("frame"),
            })
    series.sort(key=lambda x: (x.get("end") or "", x.get("filed") or ""), reverse=True)
    return {
        "company": co,
        "tag": tag,
        "taxonomy": taxonomy,
        "label": data.get("label"),
        "description": data.get("description"),
        "total_points": len(series),
        "observations": series[:limit],
    }


async def list_concepts(client: EdgarClient, query: str, contains: str | None = None) -> dict:
    """Which XBRL tags does this company actually report? Needed before get_concept is useful."""
    co = await resolve_company(client, query)
    cik = pad_cik(co["cik"])
    data = await client.get_json(FACTS.format(cik=cik))

    out = []
    for taxonomy, tags in data.get("facts", {}).items():
        for tag, body in tags.items():
            if contains and contains.lower() not in tag.lower():
                continue
            n = sum(len(v) for v in body.get("units", {}).values())
            out.append({
                "taxonomy": taxonomy,
                "tag": tag,
                "label": body.get("label"),
                "units": list(body.get("units", {}).keys()),
                "points": n,
            })
    out.sort(key=lambda x: -x["points"])
    return {"company": co, "total_tags": len(out), "concepts": out[:200]}


async def search_filings(
    client: EdgarClient,
    q: str,
    forms: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
) -> dict:
    params = [f"q={httpx_quote(q)}"]
    if forms:
        params.append(f"forms={httpx_quote(','.join(forms))}")
    if since:
        params.append(f"startdt={since}")
    if until:
        params.append(f"enddt={until}")
    url = f"{FTS}?{'&'.join(params)}"

    data = await client.get_json(url, use_cache=False)
    hits = data.get("hits", {})
    total = hits.get("total", {}).get("value", 0)

    out = []
    for h in hits.get("hits", [])[:limit]:
        src = h.get("_source", {})
        ident = h.get("_id", "")
        acc, _, doc = ident.partition(":")
        ciks = src.get("ciks") or []
        row = {
            "company": (src.get("display_names") or [None])[0],
            "form": src.get("file_type"),
            "filed": src.get("file_date"),
            "accession": acc,
        }
        if ciks and doc:
            row["url"] = ARCHIVE.format(
                cik=int(ciks[0]), accession=acc.replace("-", ""), doc=doc
            )
        out.append(row)
    return {"query": q, "total_matches": total, "returned": len(out), "results": out}


async def compare_concept(
    client: EdgarClient, tag: str, period: str, unit: str = "USD",
    taxonomy: str = "us-gaap", limit: int = 25,
) -> dict:
    """One metric across every filer for a period. `period` looks like CY2023Q1I or CY2023."""
    url = FRAMES.format(taxonomy=taxonomy, tag=tag, unit=unit, period=period)
    data = await client.get_json(url)
    rows = sorted(data.get("data", []), key=lambda r: -(r.get("val") or 0))
    return {
        "tag": tag,
        "period": period,
        "unit": unit,
        "label": data.get("label"),
        "total_filers": len(rows),
        "top": [
            {"cik": r.get("cik"), "name": r.get("entityName"), "value": r.get("val")}
            for r in rows[:limit]
        ],
    }


def httpx_quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")
