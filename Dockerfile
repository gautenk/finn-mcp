FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so they are cached independently of the source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH" \
    FINN_MCP_TRANSPORT=streamable-http \
    FINN_MCP_HOST=0.0.0.0 \
    FINN_MCP_PORT=8000 \
    XDG_DATA_HOME=/data

VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "-m", "finn_mcp"]
