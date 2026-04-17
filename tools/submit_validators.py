"""Shared payload validators for the submit MCP tools.

Imported by both the in-container MCP server (``tools/submit_mcp_server.py``)
and the host-side harness (``harness/submit_tools.py``) so validation logic
is defined exactly once.

Only depends on stdlib + jsonschema (optional). Lives in ``tools/`` so
``docker/Dockerfile`` ships it into the container with the MCP server.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from submit_schemas import TOOL_SPECS

try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False


@dataclass
class ValidationError:
    code: str
    path: str
    hint: str


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError] = field(default_factory=list)

    def to_feedback(self, attempt: int = 1, attempts_remaining: int = 0) -> dict[str, Any]:
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


def _schema_validate(payload: Any, schema: dict) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if not _HAS_JSONSCHEMA:
        return errors
    validator = jsonschema.Draft7Validator(schema)
    for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path)):
        path = "/" + "/".join(str(p) for p in err.absolute_path) if err.absolute_path else "/"
        errors.append(ValidationError(
            code="SCHEMA_ERROR",
            path=path,
            hint=err.message,
        ))
    return errors


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


_SEMANTIC_VALIDATORS = {
    "submit_audit_report": _validate_audit_semantics,
    "submit_judge_verdict": _validate_judge_semantics,
}


def _validate(payload: Any, tool_name: str) -> ValidationResult:
    if tool_name not in TOOL_SPECS:
        return ValidationResult(ok=False, errors=[
            ValidationError(code="UNKNOWN_TOOL", path="/", hint=f"Unknown tool: {tool_name}")
        ])
    if not isinstance(payload, dict):
        return ValidationResult(ok=False, errors=[
            ValidationError(code="SCHEMA_ERROR", path="/", hint="Payload must be a JSON object.")
        ])
    schema = TOOL_SPECS[tool_name]["schema"]
    semantic_fn = _SEMANTIC_VALIDATORS[tool_name]
    errors = _schema_validate(payload, schema) + semantic_fn(payload)
    return ValidationResult(ok=not errors, errors=errors)


def validate_audit_report(payload: Any) -> ValidationResult:
    """Full validation pipeline for submit_audit_report payload."""
    return _validate(payload, "submit_audit_report")


def validate_judge_verdict(payload: Any) -> ValidationResult:
    """Full validation pipeline for submit_judge_verdict payload."""
    return _validate(payload, "submit_judge_verdict")


def validate_by_tool(tool_name: str, payload: Any) -> ValidationResult:
    """Dispatch validation by tool name (accepts bare or MCP-namespaced name)."""
    # Strip mcp__<server>__ prefix if present
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) == 3:
            tool_name = parts[2]
    return _validate(payload, tool_name)
