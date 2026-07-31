FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/asp53826/edgar-mcp" \
      org.opencontainers.image.description="Local MCP server for SEC EDGAR filings and XBRL financial facts" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 edgar \
    && useradd --system --uid 10001 --gid edgar --create-home edgar

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir .

USER edgar

ENTRYPOINT ["edgar-mcp"]
