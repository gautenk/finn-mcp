# finn-mcp

MCP server that exposes [finn.no](https://www.finn.no) — Norway's largest
online classifieds marketplace — to Claude across four verticals:

- **BAP / Torget** — used goods
- **Real estate** — homes for sale and rentals
- **Cars** — used (new cars are only available via the official API)
- **Jobs** — full-time and part-time listings (`job_type`)

## Tools

| Tool | Purpose |
|------|---------|
| `search_finn` | Search a vertical by keyword + optional filters. |
| `get_listing` | Fetch a full listing by `finnkode`. |
| `job_locations` | Look up area names usable as `location` in job searches. |
| `save_search` | Persist a named recurring search. |
| `list_saved_searches` | List all saved searches. |
| `delete_saved_search` | Remove a saved search. |
| `check_saved_search` | Run a saved search and return only hits that are new since the last check. |

`search_finn` and `save_search` take `job_type` for the `jobs` vertical:
`"fulltime"`, `"parttime"` or `"all"` (default). `check_saved_search` reuses
the stored value.

Job searches can be filtered by area with `location`, by name: a county
(`"Vestland"`), a municipality (`"Bergen"`), an Oslo district
(`"Grünerløkka"`) or a list of several. Names are resolved to finn.no's area
codes; `job_locations` lists the valid ones.

Search results carry what the result card shows, so a full listing fetch is
only needed for the ad body: `location` for every vertical, plus `extra`
fields such as `employer`, `deadline` and `positions` (jobs), `trade_type` and
`seller_type` (Torget), `seller_name`, `transmission` and `warranty_months`
(cars), and `total_price_nok`, `common_cost_monthly_nok`, `ownership_type`,
`broker` and `viewing` (real estate).

## Install & run (recommended)

Requires [uv](https://docs.astral.sh/uv/). Then use `uvx` to run the server
without cloning or installing anything permanently:

```bash
uvx finn-mcp
```

## Register with Claude Code

```bash
claude mcp add finn-mcp -- uvx finn-mcp
```

Or add to your `.mcp.json` (or Claude Desktop's `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "finn-mcp": {
      "command": "uvx",
      "args": ["finn-mcp"]
    }
  }
}
```

## HTTP transport and Docker

The server speaks stdio by default. Set `FINN_MCP_TRANSPORT=streamable-http`
to serve native streamable HTTP at `/mcp` instead:

| Variable | Default | Purpose |
|----------|---------|---------|
| `FINN_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `FINN_MCP_HOST` | `127.0.0.1` | Bind address |
| `FINN_MCP_PORT` | `8000` | Bind port |
| `FINN_MCP_ALLOWED_HOSTS` | – | Extra comma-separated `Host` values to accept |
| `FINN_MCP_ALLOWED_ORIGINS` | – | Extra comma-separated `Origin` values to accept |

`Host` headers are validated (DNS-rebinding protection). `finn.gekkomedia.no`,
`nginx`, `finn-mcp:8000` and localhost are accepted out of the box.

```bash
docker build -t finn-mcp .
docker run --rm -p 8000:8000 -v finn-data:/data finn-mcp
```

The image runs the HTTP transport on `0.0.0.0:8000` and stores the cache and
saved searches under `XDG_DATA_HOME=/data`. Check a running server with:

```bash
python -m finn_mcp.smoke --url http://localhost:8000/mcp [--host finn.gekkomedia.no]
```

It runs `initialize`, `tools/list` and one part-time job search in Oslo
(one request to finn.no) and exits non-zero on failure.

## Data access

There is no self-serve public finn.no API. For v1 this server scrapes the
public web pages and parses JSON-LD on detail pages. A stable `FinnBackend`
interface separates the MCP surface from the data source, so the scraper can
be swapped for the official partner API (`cache.api.finn.no/iad/`) later.

Backend selection is controlled by the `FINN_BACKEND` environment variable:

- `FINN_BACKEND=scraper` (default) — the scraper implementation.
- `FINN_BACKEND=official` — stub; raises `NotImplementedError` until
  partner credentials are wired up.

Responses are cached for 24 hours in a local SQLite database at
`$XDG_DATA_HOME/finn-mcp/cache.sqlite` (defaults to
`~/.local/share/finn-mcp/cache.sqlite`).

## Develop from source

```bash
git clone https://github.com/aHk-coder/finn-mcp
cd finn-mcp
uv sync
uv run pytest
uv run finn-mcp   # stdio server
```

Tests run against saved HTML fixtures in `tests/fixtures/` and do not hit
finn.no over the network.

## License

MIT. See [LICENSE](./LICENSE).
