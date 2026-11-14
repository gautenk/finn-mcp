from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURE_DIR


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Tests must never reach finn.no; a test that fetches has to stub it."""
    from finn_mcp import http_client

    async def refuse():
        raise RuntimeError("network access is disabled in tests")

    monkeypatch.setattr(http_client, "get_client", refuse)


def read_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")
