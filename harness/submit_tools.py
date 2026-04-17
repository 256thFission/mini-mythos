"""Host-side wiring for the submit MCP tools.

Two finalisation tools used by the audit and judge agents:
  - submit_audit_report
  - submit_judge_verdict

Tool metadata (descriptions + JSON Schemas) lives in ``tools/submit_schemas.py``;
validation logic lives in ``tools/submit_validators.py``. Both modules are
shared with the in-container MCP server (``tools/submit_mcp_server.py``).
This module adds host-only concerns:
  1. MCP config builder + tool-name namespacing
  2. Re-exports of the validators for host-side re-checking
  3. Fallback payloads when no valid submission is received
"""

from __future__ import annotations

import sys
from pathlib import Path

# Share tool metadata + validators with the in-container MCP server.
# ``tools/`` is the single source of truth and is copied into the image by
# ``docker/Dockerfile``. Both modules are imported here unchanged.
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from submit_schemas import TOOL_SPECS  # noqa: E402,F401
from submit_validators import (  # noqa: E402
    ValidationError,
    ValidationResult,
    validate_audit_report,
    validate_judge_verdict,
    validate_by_tool,
)

__all__ = [
    "TOOL_SPECS",
    "ValidationError",
    "ValidationResult",
    "validate_audit_report",
    "validate_judge_verdict",
    "validate_by_tool",
    "submit_tool_name",
    "build_submit_mcp_config",
    "audit_fallback",
    "judge_fallback",
    "SUBMIT_MCP_SERVER_NAME",
]


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
