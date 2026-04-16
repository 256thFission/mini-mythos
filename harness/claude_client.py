"""Centralized Claude CLI client.

Eliminates duplicated subprocess.run(["claude", ...]) logic across runner.py,
scorer.py, and validator.py. Handles standard env stripping, timeouts, and
structured error returns.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ClaudeResult:
    """Structured result from a Claude CLI invocation."""

    stdout: str
    stderr: str
    returncode: int
    error: str | None = None  # Set if timeout or exception occurred
    # Parsed from stream-json output
    events: list[dict] | None = None
    full_text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list[dict] | None = None
    result_subtype: str = ""  # e.g. "success", "error_max_turns"
    session_id: str = ""


def _strip_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return env dict with CLAUDE_CODE* and CLAUDECODE vars removed."""
    if env is None:
        env = dict(os.environ)
    return {
        k: v for k, v in env.items()
        if not k.startswith("CLAUDE_CODE") and k != "CLAUDECODE"
    }


def _build_claude_args(
    prompt: str,
    model: str,
    output_format: str = "stream-json",
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    resume_session_id: str | None = None,
    tools: list[dict] | None = None,
) -> list[str]:
    """Build the base claude CLI arguments."""
    import json as _json
    cmd = ["claude"]
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])
        if prompt:
            cmd.extend(["-p", prompt])
        cmd.extend(["--model", model, "--output-format", output_format,
                    "--dangerously-skip-permissions"])
    else:
        cmd.extend(["-p", prompt, "--model", model, "--output-format", output_format,
                    "--dangerously-skip-permissions"])
    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])
    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    if tools:
        cmd.extend(["--tools", _json.dumps(tools)])
    return cmd


def invoke_claude(
    prompt: str,
    model: str,
    timeout: int,
    output_format: str = "stream-json",
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    use_docker: bool = False,
    container_name: str | None = None,
    container_workdir: str | None = None,
    container_home: str = "/audit-home",
    claude_home: str | None = None,
    verbose: bool = False,
    resume_session_id: str | None = None,
    tools: list[dict] | None = None,
) -> ClaudeResult:
    """Invoke the claude CLI and return a structured result.

    Args:
        prompt: The prompt to send to claude
        model: Model name (e.g., "claude-opus-4-6")
        timeout: Timeout in seconds
        output_format: "stream-json" or "json"
        max_turns: Maximum turns for agentic mode
        max_budget_usd: Maximum budget for this invocation
        use_docker: If True, wrap in docker exec
        container_name: Docker container name (required if use_docker)
        container_workdir: Working directory inside container
        claude_home: HOME directory to use (for auth context)
        verbose: Add --verbose flag

    Returns:
        ClaudeResult with stdout, stderr, returncode, and parsed fields.
        If timeout or exception, error field is set and returncode is -1.
    """
    base_cmd = _build_claude_args(
        prompt=prompt,
        model=model,
        output_format=output_format,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        resume_session_id=resume_session_id,
        tools=tools,
    )
    if verbose:
        base_cmd.append("--verbose")

    if use_docker:
        if container_name is None:
            raise ValueError("container_name required when use_docker=True")
        if container_workdir is None:
            raise ValueError("container_workdir required when use_docker=True")
        workdir = container_workdir
        # Inside the container, HOME is set to container_home (where
        # copy_claude_auth places the credentials). claude_home is a host path.
        cmd = [
            "docker", "exec", "-i",
            "-u", "audit",
            "-w", workdir,
            "-e", f"HOME={container_home}",
            container_name,
        ] + base_cmd
    else:
        cmd = base_cmd

    env = _strip_env()
    if claude_home and not use_docker:
        env["HOME"] = claude_home

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        result = ClaudeResult(
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            returncode=proc.returncode,
        )

        # Parse stream-json output if applicable
        if output_format == "stream-json":
            parsed = _parse_stream_json(result.stdout)
            result.events = parsed["events"]
            result.full_text = parsed["full_text"]
            result.cost_usd = parsed["cost_usd"]
            result.input_tokens = parsed["input_tokens"]
            result.output_tokens = parsed["output_tokens"]
            result.tool_calls = parsed["tool_calls"]
            result.result_subtype = parsed["result_subtype"]
            result.session_id = parsed["session_id"]
        elif output_format == "json":
            # Parse simple JSON envelope
            try:
                data = __import__("json").loads(result.stdout)
                result.cost_usd = float(data.get("total_cost_usd", 0.0) or 0.0)
                result.full_text = data.get("result", result.stdout)
                usage = data.get("usage", {}) or {}
                result.input_tokens = int(usage.get("input_tokens", 0))
                result.output_tokens = int(usage.get("output_tokens", 0))
            except (__import__("json").JSONDecodeError, TypeError):
                pass

        return result

    except subprocess.TimeoutExpired as e:
        return ClaudeResult(
            stdout=e.stdout or "",
            stderr=e.stderr or "",
            returncode=-1,
            error=f"timeout after {timeout}s",
        )
    except Exception as e:
        return ClaudeResult(
            stdout="",
            stderr="",
            returncode=-1,
            error=str(e),
        )


def _parse_stream_json(raw_stdout: str) -> dict[str, Any]:
    """Parse --output-format stream-json output into structured dict.

    Returns dict with:
      - events: list of parsed event dicts
      - full_text: concatenated assistant text
      - cost_usd: total cost
      - input_tokens / output_tokens
      - tool_calls: list of {name, input, result}
    """
    json_mod = __import__("json")
    events = []
    full_text_parts = []
    cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    tool_calls = []
    result_subtype = ""
    session_id = ""
    pending_tool_uses: dict[str, dict] = {}

    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json_mod.loads(line)
        except json_mod.JSONDecodeError:
            events.append({"type": "raw_line", "data": line})
            continue

        events.append(event)
        etype = event.get("type", "")

        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                btype = block.get("type", "")
                if btype == "text":
                    full_text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tid = block.get("id", "")
                    pending_tool_uses[tid] = {
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                    }

        elif etype == "tool_result":
            tid = event.get("tool_use_id", "")
            tool_info = pending_tool_uses.pop(tid, {})
            tool_calls.append({
                "tool_use_id": tid,
                "name": tool_info.get("name", "unknown"),
                "input": tool_info.get("input", {}),
                "result": event.get("content", ""),
                "is_error": event.get("is_error", False),
            })

        elif etype == "result":
            cost_usd = float(event.get("total_cost_usd", 0.0) or 0.0)
            usage = event.get("usage", {}) or {}
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            session_id = event.get("session_id", "")
            result_text = event.get("result", "")
            if result_text:
                full_text_parts.append(result_text)
            if event.get("is_error"):
                result_subtype = "error_api_terminated"
            else:
                result_subtype = event.get("subtype", "")

    return {
        "events": events,
        "full_text": "\n".join(full_text_parts),
        "cost_usd": cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool_calls": tool_calls,
        "result_subtype": result_subtype,
        "session_id": session_id,
    }
