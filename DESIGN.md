# Design notes

Two decisions here came out of the benchmark contradicting the design, not out
of planning. Both are worth writing down because the wrong version looked
correct from the outside.

## Caching: EDGAR is not one API

The first cache did what you'd write from habit — store the body with its
`ETag` and `Last-Modified`, then revalidate with `If-None-Match` /
`If-Modified-Since` and treat a `304` as a hit. Clean, standard, and on this API
almost entirely useless.

Measured behaviour of the initial version:

```
submissions  cold  0.16 MB / 133 ms
submissions  warm  0.16 MB /  37 ms     <- "warm"
companyfacts cold  3.75 MB / 121 ms
companyfacts warm  3.75 MB /  82 ms     <- "warm"
```

The warm read transferred exactly as many bytes as the cold one. Checking the
headers directly explains it:

| Host | `ETag` | `Last-Modified` |
|---|---|---|
| `data.sec.gov` | absent | absent |
| `www.sec.gov/Archives` | absent | present |

`data.sec.gov` — submissions, companyfacts, companyconcept, frames — sends no
validators at all. A conditional request there always returns a full `200`, so
the cache was structurally incapable of ever hitting. It looked like a cache
because it had a hit counter.

The fix splits freshness by what the host can actually support:

- **`/Archives/` documents are immutable.** Once an accession is accepted, that
  file never changes. It doesn't need revalidation, conditional or otherwise —
  a cached copy is correct forever. This is the largest win, because filing
  documents are also the largest objects (a 10-K is 1.5–9 MB).
- **`data.sec.gov` gets a TTL**, defaulting to one hour. There is no better
  option; this is a bounded-staleness tradeoff, not a correctness guarantee, and
  the README says so.
- **Conditional revalidation is kept** for the stale path, since Archives does
  send `Last-Modified` and a `304` is still cheaper than a body.

Post-fix, warm reads transfer zero bytes: 68× on companyfacts, 138× on a 10-K.

## Rate limiting: averaging under the limit is not staying under it

SEC publishes a 10 req/s ceiling. The first limiter was a token bucket seeded
full at capacity 10, refilling at 10/s — which averages 10 req/s and is
therefore wrong. A full bucket releases 10 requests instantly and then sustains
10 more over the following second, so a burst puts up to 20 inside one second.

Measured, with a 1-second sliding window over 40 concurrent requests:

```
sustained rate                    13.3 req/s
peak requests in any 1s window    19        (limit 10)
```

This matters more for an MCP server than for a normal client. A model doesn't
issue requests in a smooth stream; it fans out several tool calls at once, which
is precisely the burst shape a token bucket is designed to let through.

Replaced with a strict pacer: grants are spaced `1/rate` apart, so no window of
any size can exceed the rate. The configured rate is 9 req/s rather than 10 — an
EDGAR block is sticky, and the headroom costs one request per second.

```
sustained rate                     9.2 req/s
peak requests in any 1s window    10        (limit 10)
```

## Inline XBRL

Modern filings are iXBRL: the document opens with a `display:none` block
containing every tagged fact. A tag-stripper that only skips `<script>` and
`<style>` yields this as the first 200 characters of Apple's FY2025 10-K:

```
false2025FY0000320193P1YP1YP1YP1Yhttp://fasb.org/us-gaap/2025#LongTermDebtNoncurrent...
```

11,303 characters of it, ahead of any prose. Since the windowed reader hands
back the first 40K characters by default, roughly a quarter of the first window
was machine scaffolding.

The stripper skips `ix:header`, `ix:hidden`, and any element carrying
`display:none`, tracking open skipped elements on a stack that unwinds on
mismatch — filing HTML is frequently unbalanced, and a naive depth counter
either leaks hidden content or swallows the rest of the document.

## Filing history pagination

`submissions/CIK##########.json` returns recent filings inline and moves older
ones into `filings.files[]` as separate documents. A client reading only
`filings.recent` looks correct on every small company and silently truncates
history on large ones — the failure is invisible without a company that crosses
the boundary. `_all_filings` walks the overflow list; there's a test pinning it.

## Why windowing instead of Range requests

`get_filing_text` returns a character window with a `next_offset` cursor. The
obvious optimisation is an HTTP `Range` request so a window doesn't download the
whole filing, and EDGAR does support it (`206`, `accept-ranges: bytes`).

It isn't used, because the offsets are character offsets into *extracted text*
and Range operates on *bytes of source HTML*. There's no mapping between the two
without parsing the document, and partial HTML doesn't parse reliably. Given
Archives documents are cached permanently, the whole-document fetch is paid once
per filing and every subsequent window is a disk read.

## Error handling

Tools return `{"error": "..."}` rather than raising. An MCP error is a dead end
for the caller; a message in the payload lets the model correct itself — most
usefully on ambiguous company names, where the error carries the candidate list
instead of guessing.
