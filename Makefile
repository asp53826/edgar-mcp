.PHONY: test bench run lint clean

test:
	uv run pytest -q

bench:
	uv run python bench/bench.py

run:
	uv run edgar-mcp

clean:
	rm -rf .pytest_cache **/__pycache__ ~/.cache/edgar-mcp
