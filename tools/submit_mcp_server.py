#!/usr/bin/env python3
"""Minimal MCP stdio server for submit_audit_report / submit_judge_verdict.

Zero-dependency JSON-RPC 2.0 / MCP stdio implementation. Spawned inside the
target container by the Claude CLI via ``--mcp-config``. The server accepts any
arguments and returns a success envelope; the harness validates the payload
from the stream-json ``tool_use`` event and drives retries there.

Tool metadata (names, descriptions, schemas) lives in ``submit_schemas.py``
(sibling module) so the harness can share it without duplication.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from submit_schemas import TOOL_SPECS

SERVER_NAME = "submit"
SERVER_VERSION = "0.2.0"

TOOLS: list[dict[str, Any]] = [
    {"name": name, "description": spec["description"], "inputSchema": spec["schema"]}
    for name, spec in TOOL_SPECS.items()
]


def _write(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(req_id: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        if method == "initialize":
            _result(req_id, {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        elif is_notification:
            continue
        elif method == "tools/list":
            _result(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name", "")
            if name not in TOOL_SPECS:
                _error(req_id, -32602, f"unknown tool: {name}")
            else:
                _result(req_id, {
                    "content": [{"type": "text", "text": f"{name} received"}],
                    "isError": False,
                })
        elif method == "ping":
            _result(req_id, {})
        elif method in ("shutdown", "exit"):
            _result(req_id, {})
            if method == "exit":
                return
        else:
            _error(req_id, -32601, f"method not found: {method}")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
