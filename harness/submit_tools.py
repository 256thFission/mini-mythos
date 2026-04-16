"""Host-side wiring for the submit MCP tools.

Two finalisation tools used by the audit and judge agents:
  - submit_audit_report
  - submit_judge_verdict

Tool metadata (descriptions + JSON Schemas) is the single source of truth in
``tools/submit_schemas.py``; that file is shared with the in-container MCP
server (``tools/submit_mcp_server.py``). This module adds host-only concerns:
  1. MCP config builder + tool-name namespacing
  2. Schema validation (jsonschema) + semantic validation
  3. ValidationResult → feedback envelope for the retry loop
  4. Fallback payloads when no valid submission is received
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False

# Share tool metadata with the in-container MCP server. ``tools/submit_schemas.py``
# is the single source of truth; it's copied into the image by
# ``docker/Dockerfile`` and imported here on the host.
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from submit_schemas import TOOL_SPECS  # noqa: E402


# ── MCP server wiring ─────────────────────────────────────────────────────────
#
# Claude's ``--tools`` flag only whitelists *built-in* tools; custom tools are
# registered through an MCP stdio server (``tools/submit_mcp_server.py``) that
# the CLI spawns inside the container via ``--mcp-config``. When the agent
# invokes these tools, Claude Code namespaces them as
# ``mcp__<serverName>__<toolName>`` in the stream-json transcript — see
# ``submit_tool_name()`` below.

SUBMIT_MCP_SERVER_NAME = "submit"


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

def _validate(payload: Any, schema: dict,
              semantic_fn) -> ValidationResult:
    if not isinstance(payload, dict):
        return ValidationResult(ok=False, errors=[
            ValidationError(code="SCHEMA_ERROR", path="/", hint="Payload must be a JSON object.")
        ])
    errors = _schema_validate(payload, schema) + semantic_fn(payload)
    return ValidationResult(ok=not errors, errors=errors)


def validate_audit_report(payload: Any) -> ValidationResult:
    """Full validation pipeline for submit_audit_report payload."""
    return _validate(payload,
                     TOOL_SPECS["submit_audit_report"]["schema"],
                     _validate_audit_semantics)


def validate_judge_verdict(payload: Any) -> ValidationResult:
    """Full validation pipeline for submit_judge_verdict payload."""
    return _validate(payload,
                     TOOL_SPECS["submit_judge_verdict"]["schema"],
                     _validate_judge_semantics)


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
