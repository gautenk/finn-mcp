"""Streamable HTTP transport, env configuration and data-dir handling."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from finn_mcp import config, smoke
from finn_mcp.cache import Cache

INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


# ---------- config ----------

def test_transport_defaults_to_stdio(monkeypatch):
    monkeypatch.delenv("FINN_MCP_TRANSPORT", raising=False)
    assert config.transport() == "stdio"


def test_transport_from_env(monkeypatch):
    monkeypatch.setenv("FINN_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("FINN_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("FINN_MCP_PORT", "9001")
    assert config.transport() == "streamable-http"
    assert config.http_host() == "0.0.0.0"
    assert config.http_port() == 9001


def test_transport_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("FINN_MCP_TRANSPORT", "sse")
    with pytest.raises(ValueError):
        config.transport()


def test_allowed_hosts_cover_deploy_names(monkeypatch):
    monkeypatch.setenv("FINN_MCP_ALLOWED_HOSTS", "extra.example, other:9000")
    hosts = config.allowed_hosts()
    for name in ("finn.gekkomedia.no", "nginx", "finn-mcp:8000", "extra.example", "other:9000"):
        assert name in hosts


def test_xdg_data_home_controls_cache_path(monkeypatch, tmp_path):
    monkeypatch.delenv("FINN_CACHE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    expected = tmp_path / "data" / "finn-mcp" / "cache.sqlite"
    assert config.cache_db_path() == expected
    assert Cache().db_path == expected


async def test_saved_searches_are_stored_under_xdg_data_home(monkeypatch, tmp_path):
    from finn_mcp.models import SavedSearch

    monkeypatch.delenv("FINN_CACHE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    await Cache().save_search(SavedSearch(name="s", vertical="jobs", query=""))
    assert (tmp_path / "data" / "finn-mcp" / "cache.sqlite").is_file()


# ---------- live HTTP server (subprocess, no finn.no traffic) ----------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    port = _free_port()
    data = tmp_path_factory.mktemp("xdg")
    env = {
        **os.environ,
        "FINN_MCP_TRANSPORT": "streamable-http",
        "FINN_MCP_HOST": "127.0.0.1",
        "FINN_MCP_PORT": str(port),
        "XDG_DATA_HOME": str(data),
    }
    env.pop("FINN_CACHE_DB", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "finn_mcp"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early: {proc.stderr.read().decode()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("server did not start listening") from None
                time.sleep(0.2)
        yield {"url": f"http://127.0.0.1:{port}/mcp", "data": Path(data)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def test_http_initialize_and_tools_list(http_server, capsys):
    assert await smoke.run(http_server["url"], search=False) is True
    out = capsys.readouterr().out
    assert "OK   initialize" in out
    assert "OK   tools/list" in out
    assert "FEIL" not in out


@pytest.mark.parametrize("host", ["finn.gekkomedia.no", "nginx", "finn-mcp:8000"])
async def test_http_accepts_deploy_host_headers(http_server, host):
    assert await smoke.run(http_server["url"], host=host, search=False) is True


async def test_http_rejects_unknown_host_header(http_server, capsys):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            http_server["url"],
            json=INIT_REQUEST,
            headers={**MCP_HEADERS, "Host": "evil.example"},
        )
    assert response.status_code == 421

    assert await smoke.run(http_server["url"], host="evil.example", search=False) is False
    assert "FEIL initialize" in capsys.readouterr().out


async def test_http_raw_initialize_and_tools_list(http_server):
    async with httpx.AsyncClient() as client:
        init = await client.post(http_server["url"], json=INIT_REQUEST, headers=MCP_HEADERS)
        assert init.status_code == 200
        assert '"serverInfo"' in init.text and "finn-mcp" in init.text

        listed = await client.post(
            http_server["url"],
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=MCP_HEADERS,
        )
        assert listed.status_code == 200
        for tool in ("search_finn", "get_listing", "save_search", "check_saved_search"):
            assert f'"{tool}"' in listed.text


def test_smoke_cli_requires_url():
    with pytest.raises(SystemExit) as exc:
        smoke.main([])
    assert exc.value.code != 0


def test_smoke_cli_exits_nonzero_when_server_unreachable(capsys):
    assert smoke.main(["--url", f"http://127.0.0.1:{_free_port()}/mcp"]) == 1
    out = capsys.readouterr().out
    assert "FEIL initialize" in out
    assert "SMOKE FEIL" in out
