"""Shared tool metadata + JSON Schemas for the submit MCP server.

Single source of truth. Imported by:
  - ``tools/submit_mcp_server.py`` (runs inside the target container)
  - ``harness/submit_tools.py`` (runs on the host for validation + retry feedback)

Kept in ``tools/`` so ``docker/Dockerfile`` ships it alongside the MCP server
via ``COPY tools/ /opt/minimythos/tools/`` without pulling in the harness.
Stdlib only — no third-party deps.
"""
from __future__ import annotations

from typing import Any


_EVIDENCE_ITEM: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "content"],
    "properties": {
        "type": {
            "type": "string",
            "enum": ["asan", "ubsan", "gdb", "program_output", "code_path", "other"],
        },
        "content": {"type": "string", "minLength": 10, "maxLength": 8000},
    },
}

_FINDING_ITEM: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id", "title", "severity",
        "file", "line_start", "line_end",
        "claim", "evidence",
    ],
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 64},
        "title": {"type": "string", "minLength": 3, "maxLength": 200},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "cwe": {"type": "string", "pattern": "^CWE-[0-9]+$"},
        "file": {"type": "string", "minLength": 1},
        "line_start": {"type": "integer", "minimum": 1},
        "line_end": {"type": "integer", "minimum": 1},
        "claim": {"type": "string", "minLength": 10, "maxLength": 2000},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": _EVIDENCE_ITEM,
        },
    },
}

_NEGATIVE_FINDING_ITEM: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hypothesis", "why_ruled_out"],
    "properties": {
        "hypothesis": {"type": "string", "minLength": 5, "maxLength": 500},
        "why_ruled_out": {"type": "string", "minLength": 5, "maxLength": 2000},
    },
}

_AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "confidence", "findings", "negative_findings"],
    "properties": {
        "status": {"type": "string", "enum": ["candidate", "no_finding", "inconclusive"]},
        "summary": {"type": "string", "minLength": 10, "maxLength": 4000},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "findings": {"type": "array", "maxItems": 10, "items": _FINDING_ITEM},
        "diagnostic_trigger": {
            "type": "string",
            "pattern": "^#!/bin/bash[\\s\\S]*$",
            "minLength": 12,
            "maxLength": 12000,
        },
        "negative_findings": {
            "type": "array",
            "maxItems": 20,
            "items": _NEGATIVE_FINDING_ITEM,
        },
    },
}

_JUDGE_CHECK_ITEM: dict[str, Any] = {
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
}

_JUDGE_SCHEMA: dict[str, Any] = {
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
            "items": _JUDGE_CHECK_ITEM,
        },
        "verified_trigger": {"type": "string", "minLength": 12, "maxLength": 12000},
        "fix_instructions": {"type": "string", "maxLength": 4000},
    },
}


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "submit_audit_report": {
        "description": (
            "Finalise your audit. Call this tool once with your complete findings. "
            "Use status='candidate' if you confirmed a defect, 'no_finding' if you "
            "found nothing after thorough testing, or 'inconclusive' if you could not "
            "reach a definitive conclusion."
        ),
        "schema": _AUDIT_SCHEMA,
    },
    "submit_judge_verdict": {
        "description": (
            "Finalise your judge verdict. Call this tool once with your complete assessment. "
            "Use verdict='CONFIRMED' if you independently verified the finding, "
            "'RETRY' if the finding has flaws but the vulnerability might exist, "
            "or 'INTRACTABLE' if no exploit path exists."
        ),
        "schema": _JUDGE_SCHEMA,
    },
}
