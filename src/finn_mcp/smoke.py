"""Smoke test for a running finn-mcp HTTP server.

    python -m finn_mcp.smoke --url http://finn-mcp:8000/mcp
    python -m finn_mcp.smoke --url http://nginx/mcp --host finn.gekkomedia.no

Runs ``initialize``, ``tools/list`` (expects ``search_finn``) and one
part-time job search in Oslo. Prints OK/FEIL per step and exits non-zero
if any step fails. The search step makes one request to finn.no.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

OSLO_LOCATION = "1.20001.20061"

STEPS = ("initialize", "tools/list", "search_finn")


def _report(step: str, ok: bool, detail: str = "") -> None:
    status = "OK  " if ok else "FEIL"
    print(f"{status} {step}" + (f" - {detail}" if detail else ""), flush=True)


def _tool_payload(result: Any) -> Any:
    """Return the decoded return value of a tool call."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    items = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            items.append(json.loads(text))
        except json.JSONDecodeError:
            items.append(text)
    if len(items) == 1 and isinstance(items[0], (list, dict)):
        return items[0]
    return items


def _root_cause(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def run(url: str, host: str | None = None, search: bool = True) -> bool:
    """Run the smoke steps against ``url``. Returns True when all pass."""
    headers = {"Host": host} if host else {}
    step = STEPS[0]
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(60.0)
        ) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    _report(
                        step,
                        True,
                        f"{init.serverInfo.name} (protocol {init.protocolVersion})",
                    )

                    step = STEPS[1]
                    names = [t.name for t in (await session.list_tools()).tools]
                    if "search_finn" not in names:
                        _report(step, False, f"search_finn missing; got {names}")
                        return False
                    _report(step, True, f"{len(names)} tools incl. search_finn")

                    if not search:
                        return True

                    step = STEPS[2]
                    result = await session.call_tool(
                        "search_finn",
                        {
                            "vertical": "jobs",
                            "query": "",
                            "job_type": "parttime",
                            "filters": {"location": OSLO_LOCATION},
                        },
                    )
                    payload = _tool_payload(result)
                    if result.isError or isinstance(payload, dict):
                        _report(step, False, f"tool error: {payload}")
                        return False
                    if not isinstance(payload, list) or not payload:
                        _report(step, False, "no part-time jobs returned for Oslo")
                        return False
                    first = payload[0]
                    title = first.get("title") if isinstance(first, dict) else first
                    _report(
                        step,
                        True,
                        f"{len(payload)} part-time jobs in Oslo, first: {title!r}",
                    )
                    return True
    except BaseException as exc:  # noqa: BLE001 - report any failure as FEIL
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        cause = _root_cause(exc)
        _report(step, False, f"{type(cause).__name__}: {cause}")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m finn_mcp.smoke", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--url", required=True, help="MCP endpoint, e.g. http://finn-mcp:8000/mcp")
    parser.add_argument("--host", help="override the Host header (e.g. when going via nginx)")
    args = parser.parse_args(argv)
    ok = asyncio.run(run(args.url, args.host))
    print("SMOKE OK" if ok else "SMOKE FEIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
