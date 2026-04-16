"""Submit tool definitions, schemas, and validators.

Two finalisation tools used by the audit and judge agents:
  - submit_audit_report
  - submit_judge_verdict

Validation pipeline per submission:
  1. Schema validation  (shape / types / basic constraints via jsonschema)
  2. Semantic validation (status/verdict rules, line ranges, uniqueness, trigger)
  3. Returns a machine-readable ValidationResult
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False


# ── MCP server wiring ─────────────────────────────────────────────────────────
#
# The submit tools are exposed to the agent via an MCP stdio server
# (``submit_mcp_server.py``) that lives inside the target container. Claude's
# ``--tools`` flag only whitelists *built-in* tools; custom tools must be
# registered through MCP. When the agent invokes these tools, Claude Code
# namespaces them as ``mcp__<serverName>__<toolName>`` in the stream-json
# transcript — see ``submit_tool_name()`` below.

SUBMIT_MCP_SERVER_NAME = "submit"
SUBMIT_MCP_SCRIPT_BASENAME = "submit_mcp_server.py"


def submit_tool_name(base_name: str) -> str:
    """Return the MCP-namespaced tool name as it appears in stream-json."""
    return f"mcp__{SUBMIT_MCP_SERVER_NAME}__{base_name}"


def build_submit_mcp_config(script_path: str) -> dict:
    """Build the --mcp-config payload that spawns the submit server.

    ``script_path`` must be the absolute path to ``submit_mcp_server.py`` *as
    seen from inside the container* (since Claude will spawn python3 there).
    """
    return {
        "mcpServers": {
            SUBMIT_MCP_SERVER_NAME: {
                "command": "python3",
                "args": [script_path],
            }
        }
    }


# ── Tool definitions (used for validation + retry feedback; NOT sent to CLI) ──

SUBMIT_AUDIT_REPORT_TOOL = {
    "name": "submit_audit_report",
    "description": (
        "Finalise your audit. Call this tool once with your complete findings. "
        "Use status='candidate' if you confirmed a defect, 'no_finding' if you "
        "found nothing after thorough testing, or 'inconclusive' if you could not "
        "reach a definitive conclusion."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "summary", "confidence", "findings", "negative_findings"],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["candidate", "no_finding", "inconclusive"],
            },
            "summary": {"type": "string", "minLength": 10, "maxLength": 4000},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "findings": {
                "type": "array",
                "maxItems": 10,
                "items": {
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
                        "severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "critical"],
                        },
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
                                    "type": {
                                        "type": "string",
                                        "enum": [
                                            "asan", "ubsan", "gdb",
                                            "program_output", "code_path", "other",
                                        ],
                                    },
                                    "content": {
                                        "type": "string",
                                        "minLength": 10,
                                        "maxLength": 8000,
                                    },
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
    },
}

SUBMIT_JUDGE_VERDICT_TOOL = {
    "name": "submit_judge_verdict",
    "description": (
        "Finalise your judge verdict. Call this tool once with your complete assessment. "
        "Use verdict='CONFIRMED' if you independently verified the finding, "
        "'RETRY' if the finding has flaws but the vulnerability might exist, "
        "or 'INTRACTABLE' if no exploit path exists."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "reasoning", "confidence", "checks"],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["CONFIRMED", "RETRY", "INTRACTABLE"],
            },
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
                        "result": {
                            "type": "string",
                            "enum": ["pass", "fail", "not_applicable"],
                        },
                        "evidence": {"type": "string", "minLength": 10, "maxLength": 3000},
                    },
                },
            },
            "verified_trigger": {
                "type": "string",
                "minLength": 12,
                "maxLength": 12000,
            },
            "fix_instructions": {"type": "string", "maxLength": 4000},
        },
    },
}


# ── Validation result ─────────────────────────────────────────────────────────

@dataclass
class ValidationError:
    code: str
    path: str
    hint: str


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError] = field(default_factory=list)

    def to_feedback(self, attempt: int, attempts_remaining: int) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_type": "VALIDATION_FAILED" if not self.ok else None,
            "message": (
                "Submission failed validation. Fix and resubmit."
                if not self.ok else "Submission accepted."
            ),
            "errors": [
                {"code": e.code, "path": e.path, "hint": e.hint}
                for e in self.errors
            ],
            "retry_allowed": attempts_remaining > 0,
            "attempt": attempt,
            "attempts_remaining": attempts_remaining,
        }


# ── Schema validation (jsonschema, with fallback) ─────────────────────────────

def _schema_validate(payload: Any, schema: dict) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if not _HAS_JSONSCHEMA:
        return errors  # skip if library absent; semantic checks still run
    validator = jsonschema.Draft7Validator(schema)
    for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path)):
        path = "/" + "/".join(str(p) for p in err.absolute_path) if err.absolute_path else "/"
        errors.append(ValidationError(
            code="SCHEMA_ERROR",
            path=path,
            hint=err.message,
        ))
    return errors


# ── Audit semantic validation ─────────────────────────────────────────────────

def _validate_audit_semantics(payload: dict) -> list[ValidationError]:
    errors: list[ValidationError] = []
    status = payload.get("status", "")
    findings = payload.get("findings", [])
    trigger = payload.get("diagnostic_trigger", "")

    if status == "candidate":
        if not findings:
            errors.append(ValidationError(
                code="SEMANTIC_VIOLATION",
                path="/findings",
                hint="status=candidate requires at least one finding.",
            ))
        if not trigger:
            errors.append(ValidationError(
                code="REQUIRED_MISSING",
                path="/diagnostic_trigger",
                hint="Required when status=candidate.",
            ))
        elif not re.match(r'^#!/bin/bash', trigger):
            errors.append(ValidationError(
                code="SEMANTIC_VIOLATION",
                path="/diagnostic_trigger",
                hint="diagnostic_trigger must start with #!/bin/bash.",
            ))
    elif status in ("no_finding", "inconclusive"):
        if findings:
            errors.append(ValidationError(
                code="SEMANTIC_VIOLATION",
                path="/findings",
                hint=f"status={status} requires findings to be empty.",
            ))

    # Per-finding checks
    seen_ids: set[str] = set()
    for i, finding in enumerate(findings):
        fid = finding.get("id", "")
        if fid in seen_ids:
            errors.append(ValidationError(
                code="SEMANTIC_VIOLATION",
                path=f"/findings/{i}/id",
                hint=f"Duplicate finding id '{fid}'.",
            ))
        seen_ids.add(fid)

        line_start = finding.get("line_start")
        line_end = finding.get("line_end")
        if isinstance(line_start, int) and isinstance(line_end, int):
            if line_end < line_start:
                errors.append(ValidationError(
                    code="SEMANTIC_VIOLATION",
                    path=f"/findings/{i}/line_end",
                    hint=f"line_end ({line_end}) must be >= line_start ({line_start}).",
                ))

    return errors


# ── Judge semantic validation ─────────────────────────────────────────────────

def _validate_judge_semantics(payload: dict) -> list[ValidationError]:
    errors: list[ValidationError] = []
    verdict = payload.get("verdict", "")
    trigger = payload.get("verified_trigger", "")
    fix = payload.get("fix_instructions", "")

    if verdict == "CONFIRMED":
        if not trigger:
            errors.append(ValidationError(
                code="REQUIRED_MISSING",
                path="/verified_trigger",
                hint="Required when verdict=CONFIRMED.",
            ))
        elif not re.match(r'^#!/bin/bash', trigger):
            errors.append(ValidationError(
                code="SEMANTIC_VIOLATION",
                path="/verified_trigger",
                hint="verified_trigger must start with #!/bin/bash.",
            ))
    elif verdict == "RETRY":
        if not fix:
            errors.append(ValidationError(
                code="REQUIRED_MISSING",
                path="/fix_instructions",
                hint="Required when verdict=RETRY.",
            ))

    return errors


# ── Public entry points ───────────────────────────────────────────────────────

def validate_audit_report(payload: Any) -> ValidationResult:
    """Full validation pipeline for submit_audit_report payload."""
    if not isinstance(payload, dict):
        return ValidationResult(ok=False, errors=[
            ValidationError(code="SCHEMA_ERROR", path="/", hint="Payload must be a JSON object.")
        ])
    schema_errors = _schema_validate(payload, SUBMIT_AUDIT_REPORT_TOOL["input_schema"])
    semantic_errors = _validate_audit_semantics(payload)
    all_errors = schema_errors + semantic_errors
    return ValidationResult(ok=not all_errors, errors=all_errors)


def validate_judge_verdict(payload: Any) -> ValidationResult:
    """Full validation pipeline for submit_judge_verdict payload."""
    if not isinstance(payload, dict):
        return ValidationResult(ok=False, errors=[
            ValidationError(code="SCHEMA_ERROR", path="/", hint="Payload must be a JSON object.")
        ])
    schema_errors = _schema_validate(payload, SUBMIT_JUDGE_VERDICT_TOOL["input_schema"])
    semantic_errors = _validate_judge_semantics(payload)
    all_errors = schema_errors + semantic_errors
    return ValidationResult(ok=not all_errors, errors=all_errors)


# ── Fallback artifacts ────────────────────────────────────────────────────────

def audit_fallback(attempt_count: int = 0, validation_errors: list[dict] | None = None) -> dict:
    return {
        "status": "inconclusive",
        "summary": "No valid submission received before deadline",
        "confidence": 0,
        "findings": [],
        "negative_findings": [],
        "_fallback": True,
        "_attempt_count": attempt_count,
        "_validation_errors": validation_errors or [],
    }


def judge_fallback() -> dict:
    return {
        "verdict": "RETRY",
        "reasoning": "No valid judge submission received before deadline",
        "confidence": 0,
        "checks": [
            {
                "name": "trigger_realism_check",
                "result": "not_applicable",
                "evidence": "No valid submission was received before the deadline.",
            }
        ],
        "fix_instructions": "Resubmit with a valid submit_judge_verdict payload.",
        "_fallback": True,
    }
