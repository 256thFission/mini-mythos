#!/usr/bin/env python3
"""MCP stdio server for submit_audit_report / submit_judge_verdict.

Built on the official ``mcp`` Python SDK (low-level ``Server`` API). Validates
payloads against the shared schemas in ``submit_schemas.py`` + semantic rules
in ``submit_validators.py``. On invalid input the tool call returns an error
result so Claude sees ``is_error=True`` and retries *inside its own session*
— the harness no longer drives schema-validation retries.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Make sibling modules importable when launched directly (no package install).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from submit_schemas import TOOL_SPECS  # noqa: E402
from submit_validators import validate_by_tool  # noqa: E402

from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
import mcp.types as types  # noqa: E402


SERVER_NAME = "submit"
server: Server = Server(SERVER_NAME)


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=spec["description"],
            inputSchema=spec["schema"],
        )
        for name, spec in TOOL_SPECS.items()
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    result = validate_by_tool(name, arguments)
    if result.ok:
        return [types.TextContent(type="text", text=f"{name} accepted")]

    feedback = result.to_feedback(attempt=1, attempts_remaining=1)
    # Raising here causes the SDK to return a CallToolResult with
    # isError=True and the exception message as the text content — which
    # Claude surfaces to the model as a tool_result error so it can fix and
    # resubmit without the harness having to intervene.
    raise ValueError(json.dumps(feedback, indent=2))


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except (BrokenPipeError, KeyboardInterrupt):
        pass
