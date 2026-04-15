"""ClaudeCodeAdapter — invokes the `claude` CLI non-interactively and captures
the full event stream for observability (CoT, tool calls, tool results, responses).

Uses --output-format stream-json to get per-event records.
"""

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import budget as budget_mod
from claude_client import invoke_claude
from config import config, TargetConfig
import preprocessor as preprocessor_mod


AUDIT_PROMPT_PATH = config.PROMPTS_DIR / "audit.txt"
AUDIT_MODEL = config.AUDIT_MODEL

PER_RUN_BUDGET_USD = config.PER_RUN_BUDGET_USD
RUN_TIMEOUT_SEC = config.RUN_TIMEOUT_SEC
RUN_MAX_TURNS = config.RUN_MAX_TURNS

TRANSCRIPTS_DIR = config.RUNS_DIR / "transcripts"


@dataclass
class RunResult:
    status: str
    defect_report: str = ""
    diagnostic_trigger: str = ""
    no_finding_reason: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    model: str = AUDIT_MODEL
    error_message: str = ""
    transcript_events: list = field(default_factory=list)
    session_id: str = ""


RESUME_CONTINUATION_PROMPT = (
    "The previous session was interrupted by an API error. "
    "Please continue your security analysis and provide your final JSON verdict when complete."
)


RETRY_HANDOFF_HEADER = """

--- RETRY GUIDANCE (from independent judge) ---
A previous attempt on this file was reviewed by an independent judge. Their assessment:

{retry_handoff}

Start your analysis fresh. Do NOT repeat the same approach as before.
Use the judge's guidance as your starting point. If you believe the judge is wrong,
prove it empirically with tool output.
---
"""


def _load_prompt(
    filename: str,
    target: TargetConfig,
    retry_handoff: str | None = None,
    dead_fn_annotation: str = "",
) -> str:
    template = AUDIT_PROMPT_PATH.read_text()
    prompt = (
        template
        .replace("{filename}", filename)
        .replace("{project_name}", target.name)
        .replace("{project_description}", target.description)
        .replace("{source_dir}", target.container_workdir)
        .replace("{dead_functions}", dead_fn_annotation)
    )
    if retry_handoff:
        prompt = prompt + RETRY_HANDOFF_HEADER.replace("{retry_handoff}", retry_handoff)
    return prompt


def _parse_json_output(text: str) -> tuple[str, str, str, str]:
    """Parse auditor output. Tries strict JSON first, falls back to XML tag extraction.

    Returns (status, defect_report, diagnostic_trigger, no_finding_reason).
    Falls back to ("declined", "", "", reason) if nothing can be parsed.
    """
    stripped = text.strip()

    # ── Attempt 1: strict JSON (preferred) ──────────────────────────────────
    # Find all top-level '{' positions and try each as a JSON start, last-first,
    # so we pick the final/authoritative copy when the agent duplicates output.
    for m in reversed(list(re.finditer(r'\{', stripped))):
        candidate = stripped[m.start():]
        try:
            data = json.loads(candidate)
            status = data.get("status", "")
            defect_report = data.get("defect_report", "")
            diagnostic_trigger = data.get("diagnostic_trigger", "")
            no_finding_reason = data.get("no_finding_reason", "")
            if status in ("candidate", "no_finding"):
                return status, defect_report, diagnostic_trigger, no_finding_reason
        except json.JSONDecodeError:
            pass

    # ── Attempt 2: XML tag extraction (legacy agent format) ──────────────────
    dr = re.search(r"<defect_report>(.*?)</defect_report>", stripped, re.DOTALL)
    dt = re.search(r"<diagnostic_trigger>(.*?)</diagnostic_trigger>", stripped, re.DOTALL)
    nf = re.search(r"<no_finding>(.*?)</no_finding>", stripped, re.DOTALL)

    defect_report = dr.group(1).strip() if dr else ""
    diagnostic_trigger = dt.group(1).strip() if dt else ""
    no_finding_reason = nf.group(1).strip() if nf else ""

    if defect_report and diagnostic_trigger:
        return "candidate", defect_report, diagnostic_trigger, ""
    if no_finding_reason:
        return "no_finding", "", "", no_finding_reason

    return "declined", "", "", f"json_parse_failure: no parseable output in {len(stripped)} chars"




def _save_transcript(run_id: str, filename: str, status: str, events: list, tool_calls: list) -> Path:
    """Write a human-readable + machine-parseable transcript for a run."""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = TRANSCRIPTS_DIR / f"{filename}__{status}__{run_id[:8]}.jsonl"

    with open(transcript_path, "w") as f:
        # Header
        f.write(json.dumps({
            "type": "header",
            "run_id": run_id,
            "target_file": filename,
        }) + "\n")
        # All stream events
        for event in events:
            f.write(json.dumps(event) + "\n")
        # Tool call summary
        f.write(json.dumps({
            "type": "tool_call_summary",
            "tool_calls": tool_calls,
        }) + "\n")

    return transcript_path


def run_audit(
    source_dir: str | Path,
    filename: str,
    file_score: int,
    run_id: str,
    tracker: budget_mod.BudgetTracker,
    target: TargetConfig,
    harness_flags: list[str] | None = None,
    model: str = AUDIT_MODEL,
    claude_home: str | None = None,
    retry_handoff: str | None = None,
    retry_number: int = 0,
    resume_session_id: str | None = None,
) -> RunResult:
    """Execute one audit run via the claude CLI. Returns a populated RunResult."""
    source_dir = Path(source_dir)
    if harness_flags is None:
        harness_flags = []

    compiled_symbols = preprocessor_mod.load_symbols_for_file(filename, target)
    dead_fn_annotation = preprocessor_mod.dead_function_annotation(
        Path(source_dir) / filename, compiled_symbols
    )
    prompt = _load_prompt(
        filename, target=target,
        retry_handoff=retry_handoff,
        dead_fn_annotation=dead_fn_annotation,
    )

    # Reset container workdir to clean state before each run so test artifacts
    # from prior runs don't bias the agent toward already-explored paths.
    subprocess.run(
        ["docker", "exec", target.container_name, "bash", "-c",
         f"cd {target.container_workdir} && git clean -fdx --quiet"],
        capture_output=True,
    )

    start = time.time()

    # Use centralized claude_client
    claude_result = invoke_claude(
        prompt=RESUME_CONTINUATION_PROMPT if resume_session_id else prompt,
        model=model,
        timeout=RUN_TIMEOUT_SEC,
        output_format="stream-json",
        max_turns=RUN_MAX_TURNS,
        max_budget_usd=PER_RUN_BUDGET_USD,
        use_docker=True,
        container_name=target.container_name,
        container_workdir=target.container_workdir,
        container_home=config.CONTAINER_HOME,
        claude_home=claude_home,
        verbose=True,
        resume_session_id=resume_session_id,
    )

    duration = time.time() - start

    # Handle errors (timeout, exception)
    if claude_result.error:
        result = RunResult(
            status="error",
            raw_stdout=claude_result.stdout[:50_000],
            raw_stderr=claude_result.stderr[:10_000],
            duration_seconds=round(duration, 1),
            error_message=claude_result.error,
        )
        _log_run(run_id, filename, file_score, model, result, tracker, harness_flags, target=target)
        return result

    # Extract parsed fields
    agent_text = claude_result.full_text
    cost_usd = claude_result.cost_usd
    in_tok = claude_result.input_tokens
    out_tok = claude_result.output_tokens

    # Detect turn-limit exhaustion: agent used all turns without producing output.
    if claude_result.result_subtype == "error_max_turns":
        result = RunResult(
            status="error",
            raw_stdout=claude_result.stdout[:50_000],
            raw_stderr=claude_result.stderr[:10_000],
            duration_seconds=round(duration, 1),
            error_message="max_turns_exceeded",
        )
        _log_run(run_id, filename, file_score, model, result, tracker, harness_flags, target=target)
        return result

    # Detect API-level session termination (is_error=True on result event).
    if claude_result.result_subtype == "error_api_terminated":
        result = RunResult(
            status="error",
            raw_stdout=claude_result.stdout[:50_000],
            raw_stderr=claude_result.stderr[:10_000],
            duration_seconds=round(duration, 1),
            error_message="api_terminated",
            session_id=claude_result.session_id,
        )
        _log_run(run_id, filename, file_score, model, result, tracker, harness_flags, target=target)
        return result

    # Detect usage/rate-limit: either zero tokens (clean rejection) or the
    # API injected its limit message into the result text mid-session.
    _RATE_LIMIT_PHRASES = ("you've hit your limit", "you have hit your limit", "rate limit")
    _at_lower = agent_text.strip().lower()
    if (in_tok == 0 and out_tok == 0 and cost_usd == 0.0 and not agent_text.strip()) or \
            (len(_at_lower) < 200 and any(p in _at_lower for p in _RATE_LIMIT_PHRASES)):
        result = RunResult(
            status="error",
            raw_stdout=claude_result.stdout[:50_000],
            raw_stderr=claude_result.stderr[:10_000],
            duration_seconds=round(duration, 1),
            error_message="usage_limit",
        )
        _log_run(run_id, filename, file_score, model, result, tracker, harness_flags, target=target)
        return result

    tool_calls = claude_result.tool_calls or []
    events = claude_result.events or []

    # Parse JSON output from agent
    status, defect_report, diagnostic_trigger, no_finding_reason = _parse_json_output(agent_text)

    # Re-classify if needed based on content
    if status == "declined" and (defect_report or no_finding_reason):
        # JSON parse failed but we have content
        pass
    elif status == "candidate" and not (defect_report and diagnostic_trigger):
        # Incomplete candidate
        status = "declined"
        no_finding_reason = "incomplete_output: missing defect_report or diagnostic_trigger"

    # Save per-run transcript for all runs so agent output is never lost
    transcript_path = _save_transcript(run_id, filename, status, events, tool_calls)

    try:
        cumulative = tracker.record(cost_usd)
    except budget_mod.BudgetExceededError:
        cumulative = tracker.spent()

    result = RunResult(
        status=status,
        defect_report=defect_report,
        diagnostic_trigger=diagnostic_trigger,
        no_finding_reason=no_finding_reason,
        raw_stdout=claude_result.stdout[:50_000],
        raw_stderr=claude_result.stderr[:10_000],
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost_usd,
        duration_seconds=round(duration, 1),
        model=model,
        transcript_events=events,
    )

    _log_run(
        run_id, filename, file_score, model, result, tracker, harness_flags,
        target=target,
        cumulative_cost=cumulative,
        transcript_path=str(transcript_path) if transcript_path else None,
        tool_call_count=len(tool_calls),
        retry_number=retry_number,
    )
    return result


def _log_run(
    run_id: str,
    target_file: str,
    file_score: int,
    model: str,
    result: RunResult,
    tracker: budget_mod.BudgetTracker,
    harness_flags: list[str],
    target: TargetConfig | None = None,
    cumulative_cost: float | None = None,
    transcript_path: str | None = None,
    tool_call_count: int = 0,
    retry_number: int = 0,
) -> None:
    """Append run record to per-target audit.jsonl."""
    from datetime import datetime, timezone

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "provider": "claude_code",
        "model": model,
        "prompt_version": "audit_v1",
        "target_file": target_file,
        "repo_revision": target.repo_revision if target else "",
        "container_image": target.container_image if target else "",
        "harness_flags": harness_flags,
        "file_score": file_score,
        "status": result.status,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_usd": result.cost_usd,
        "cumulative_cost_usd": cumulative_cost if cumulative_cost is not None else tracker.spent(),
        "duration_seconds": result.duration_seconds,
        "tool_call_count": tool_call_count,
        "transcript_path": transcript_path,
        "retry_number": retry_number,
        "validation_verdict": None,
        "asan_triggered": None,
        "defect_report": result.defect_report or None,
        "diagnostic_trigger": result.diagnostic_trigger or None,
        "no_finding_reason": result.no_finding_reason or None,
        "error_message": result.error_message or None,
        "session_id": result.session_id or None,
        "raw_stderr": result.raw_stderr,
        # raw_stdout not stored in JSONL (see transcript file instead)
    }

    audit_log = config.audit_log_path(target.name) if target else config.AUDIT_LOG
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_log, "a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
