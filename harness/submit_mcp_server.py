#!/usr/bin/env python3
"""Minimal MCP stdio server exposing submit_audit_report and submit_judge_verdict.

This is a zero-dependency implementation of the JSON-RPC 2.0 / Model Context
Protocol stdio transport. It runs *inside the target container* — spawned by
the Claude CLI via ``--mcp-config``. The server does not validate the payload
semantics; it simply accepts the call and returns a success envelope. The
harness observes the invocation through the stream-json ``tool_use`` event
and runs full validation there.

Protocol flow implemented:
  - initialize          → capability handshake
  - notifications/initialized → ack (no response)
  - tools/list          → enumerate the two submit tools
  - tools/call          → always returns a success message
  - ping                → {}
  - unknown methods     → JSON-RPC error -32601

Invocation (by Claude CLI):
  python3 submit_mcp_server.py
"""
from __future__ import annotations

import json
import sys
from typing import Any


SERVER_NAME = "submit"
SERVER_VERSION = "0.1.0"

# Tool schemas are intentionally duplicated here (not imported from
# submit_tools.py) so this file can be copied standalone into the container
# without dragging the rest of the harness with it.

SUBMIT_AUDIT_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "confidence", "findings", "negative_findings"],
    "properties": {
        "status": {"type": "string", "enum": ["candidate", "no_finding", "inconclusive"]},
        "summary": {"type": "string", "minLength": 10, "maxLength": 4000},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "findings": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "severity", "file",
                             "line_start", "line_end", "claim", "evidence"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "title": {"type": "string", "minLength": 3, "maxLength": 200},
                    "severity": {"type": "string",
                                 "enum": ["low", "medium", "high", "critical"]},
                    "cwe": {"type": "string", "pattern": "^CWE-[0-9]+$"},
                    "file": {"type": "string", "minLength": 1},
                    "line_start": {"type": "integer", "minimum": 1},
                    "line_end": {"type": "integer", "minimum": 1},
                    "claim": {"type": "string", "minLength": 10, "maxLength": 2000},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 10,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "content"],
                            "properties": {
                                "type": {"type": "string",
                                         "enum": ["asan", "ubsan", "gdb",
                                                  "program_output", "code_path", "other"]},
                                "content": {"type": "string", "minLength": 10, "maxLength": 8000},
                            },
                        },
                    },
                },
            },
        },
        "diagnostic_trigger": {
            "type": "string",
            "pattern": "^#!/bin/bash[\\s\\S]*$",
            "minLength": 12,
            "maxLength": 12000,
        },
        "negative_findings": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["hypothesis", "why_ruled_out"],
                "properties": {
                    "hypothesis": {"type": "string", "minLength": 5, "maxLength": 500},
                    "why_ruled_out": {"type": "string", "minLength": 5, "maxLength": 2000},
                },
            },
        },
    },
}

SUBMIT_JUDGE_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reasoning", "confidence", "checks"],
    "properties": {
        "verdict": {"type": "string", "enum": ["CONFIRMED", "RETRY", "INTRACTABLE"]},
        "reasoning": {"type": "string", "minLength": 20, "maxLength": 5000},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "checks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "result", "evidence"],
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": [
                            "source_guard_check",
                            "object_symbol_check",
                            "cross_package_build_check",
                            "call_path_reachability_check",
                            "trigger_realism_check",
                        ],
                    },
                    "result": {"type": "string", "enum": ["pass", "fail", "not_applicable"]},
                    "evidence": {"type": "string", "minLength": 10, "maxLength": 3000},
                },
            },
        },
        "verified_trigger": {"type": "string", "minLength": 12, "maxLength": 12000},
        "fix_instructions": {"type": "string", "maxLength": 4000},
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "submit_audit_report",
        "description": (
            "Finalise your audit. Call this tool once with your complete findings. "
            "Use status='candidate' if you confirmed a defect, 'no_finding' if you "
            "found nothing after thorough testing, or 'inconclusive' if you could not "
            "reach a definitive conclusion."
        ),
        "inputSchema": SUBMIT_AUDIT_REPORT_SCHEMA,
    },
    {
        "name": "submit_judge_verdict",
        "description": (
            "Finalise your judge verdict. Call this tool once with your complete assessment. "
            "Use verdict='CONFIRMED' if you independently verified the finding, "
            "'RETRY' if the finding has flaws but the vulnerability might exist, "
            "or 'INTRACTABLE' if no exploit path exists."
        ),
        "inputSchema": SUBMIT_JUDGE_VERDICT_SCHEMA,
    },
]


def _write(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(req_id: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def handle_initialize(req_id: Any, params: dict[str, Any]) -> None:
    # Echo the client's protocolVersion when present; otherwise use a sane default.
    protocol_version = params.get("protocolVersion") or "2024-11-05"
    _result(req_id, {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })


def handle_tools_list(req_id: Any) -> None:
    _result(req_id, {"tools": TOOLS})


def handle_tools_call(req_id: Any, params: dict[str, Any]) -> None:
    name = params.get("name", "")
    if name not in ("submit_audit_report", "submit_judge_verdict"):
        _error(req_id, -32602, f"unknown tool: {name}")
        return
    # We accept any arguments — validation happens harness-side from the
    # tool_use event observed in the stream-json transcript.
    _result(req_id, {
        "content": [{"type": "text", "text": f"{name} received"}],
        "isError": False,
    })


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

        # Notifications carry no id and expect no response.
        is_notification = "id" not in msg

        if method == "initialize":
            handle_initialize(req_id, params)
        elif method == "notifications/initialized" or is_notification:
            continue
        elif method == "tools/list":
            handle_tools_list(req_id)
        elif method == "tools/call":
            handle_tools_call(req_id, params)
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
