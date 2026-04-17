"""ClaudeCodeAdapter — invokes the `claude` CLI non-interactively and captures
the full event stream for observability (CoT, tool calls, tool results, responses).

Uses --output-format stream-json to get per-event records.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import budget as budget_mod
from claude_client import invoke_claude
from config import config, TargetConfig
import preprocessor as preprocessor_mod
import submit_tools as submit_mod


AUDIT_PROMPT_PATH = config.PROMPTS_DIR / "audit.txt"
AUDIT_MODEL = config.AUDIT_MODEL

PER_RUN_BUDGET_USD = config.PER_RUN_BUDGET_USD
RUN_TIMEOUT_SEC = config.RUN_TIMEOUT_SEC
RUN_MAX_TURNS = config.RUN_MAX_TURNS



@dataclass
class RunResult:
    status: str
    summary: str = ""
    confidence: float = 0.0
    findings: list = field(default_factory=list)
    negative_findings: list = field(default_factory=list)
    diagnostic_trigger: str = ""
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
    submit_attempts: int = 0
    fallback: bool = False


RESUME_CONTINUATION_PROMPT = (
    "The previous session was interrupted by an API error. "
    "Please continue your security analysis and call submit_audit_report with your best findings when complete."
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

FORCED_FINALIZATION_TEMPLATE = (
    "Finalization turn for audit of `{filename}`. Stop investigation now. "
    "Do not run additional exploratory actions, do not read git history, do not "
    "analyze other files. Call submit_audit_report immediately with your best "
    "final payload summarizing what you found (or didn't find) in `{filename}`."
)


def _load_prompt(
    filename: str,
    target: TargetConfig,
    retry_handoff: str | None = None,
    dead_fn_annotation: str = "",
) -> str:
    template = AUDIT_PROMPT_PATH.read_text()
    if target.binaries:
        binaries_block = "\n".join(f"  - {p}" for p in target.binaries)
    else:
        binaries_block = "  (none declared — inspect the build tree to find them)"
    prompt = (
        template
        .replace("{filename}", filename)
        .replace("{project_name}", target.name)
        .replace("{project_description}", target.description)
        .replace("{source_dir}", target.container_workdir)
        .replace("{binaries}", binaries_block)
        .replace("{dead_functions}", dead_fn_annotation)
    )
    if retry_handoff:
        prompt = prompt + RETRY_HANDOFF_HEADER.replace("{retry_handoff}", retry_handoff)
    return prompt





def _save_transcript(
    run_id: str, filename: str, status: str, events: list, tool_calls: list,
    target_name: str,
) -> Path:
    """Write a human-readable + machine-parseable transcript for a run."""
    transcripts_dir = config.target_runs_dir(target_name) / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcripts_dir / f"{filename}__{status}__{run_id[:8]}.jsonl"

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


_SUBMIT_AUDIT_TOOL_NAMES = {
    "submit_audit_report",
    submit_mod.submit_tool_name("submit_audit_report"),
}


def _extract_submit_payload(tool_calls: list[dict]) -> dict | None:
    """Return the input dict of the last submit_audit_report tool call, or None.

    Matches both the bare name and the MCP-namespaced name
    (``mcp__submit__submit_audit_report``) that Claude Code emits for
    MCP-registered tools.
    """
    for tc in reversed(tool_calls):
        if tc.get("name") not in _SUBMIT_AUDIT_TOOL_NAMES:
            continue
        # Skip submits the MCP server explicitly rejected — those are not real
        # submissions. A submit whose result never came back (entry still has
        # the default is_error=False placeholder) is still considered, since
        # stream-json can cut off before the result arrives.
        if tc.get("is_error"):
            continue
        payload = tc.get("input")
        if isinstance(payload, dict):
            return payload
    return None


def _run_one_audit_session(
    prompt: str,
    model: str,
    timeout: int,
    target: TargetConfig,
    claude_home: str | None,
    resume_session_id: str | None = None,
) -> tuple[object, float, int, int, list, list, str]:
    """Invoke claude once and return (claude_result, cost, in_tok, out_tok, events, tool_calls, session_id)."""
    claude_result = invoke_claude(
        prompt=prompt,
        model=model,
        timeout=timeout,
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
        mcp_config=submit_mod.build_submit_mcp_config(config.CONTAINER_MCP_SERVER_PATH),
    )
    return (
        claude_result,
        claude_result.cost_usd,
        claude_result.input_tokens,
        claude_result.output_tokens,
        claude_result.events or [],
        claude_result.tool_calls or [],
        claude_result.session_id,
    )


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

    # No need to reset the workdir between runs: the build tree is mounted
    # read-only (see docker/Dockerfile.tmpl) and the audit user cannot create
    # files inside it, so there is nothing to clean. Scratch work lives under
    # /audit-home, which each run treats as ephemeral.

    start = time.time()
    total_cost = 0.0
    total_in_tok = 0
    total_out_tok = 0
    all_events: list = []
    all_tool_calls: list = []
    accumulated_session_id = ""
    submit_attempt = 0
    max_submit_retries = config.SUBMIT_MAX_RETRIES

    # ── Main session (turns 1..max_turns) ────────────────────────────────────
    first_prompt = RESUME_CONTINUATION_PROMPT if resume_session_id else prompt
    claude_result, cost, in_tok, out_tok, events, tool_calls, sid = _run_one_audit_session(
        prompt=first_prompt,
        model=model,
        timeout=RUN_TIMEOUT_SEC,
        target=target,
        claude_home=claude_home,
        resume_session_id=resume_session_id,
    )
    total_cost += cost
    total_in_tok += in_tok
    total_out_tok += out_tok
    all_events.extend(events)
    all_tool_calls.extend(tool_calls)
    accumulated_session_id = sid or accumulated_session_id

    duration = time.time() - start

    # ── Hard-error checks (timeout, API failure, usage limit) ─────────────────
    if claude_result.error:
        result = RunResult(
            status="error",
            raw_stdout=claude_result.stdout[:50_000],
            raw_stderr=claude_result.stderr[:10_000],
            duration_seconds=round(duration, 1),
            error_message=claude_result.error,
            session_id=accumulated_session_id,
        )
        _log_run(run_id, filename, file_score, model, result, tracker, harness_flags, target=target)
        return result

    agent_text = claude_result.full_text

    if claude_result.result_subtype == "error_max_turns":
        result = RunResult(
            status="error",
            raw_stdout=claude_result.stdout[:50_000],
            raw_stderr=claude_result.stderr[:10_000],
            duration_seconds=round(duration, 1),
            error_message="max_turns_exceeded",
            session_id=accumulated_session_id,
        )
        _log_run(run_id, filename, file_score, model, result, tracker, harness_flags, target=target)
        return result

    _RATE_LIMIT_PHRASES = (
        "you've hit your limit", "you have hit your limit", "rate limit",
        "out of extra usage", "out of usage",
    )
    _at_lower = agent_text.strip().lower()
    if (in_tok == 0 and out_tok == 0 and cost == 0.0 and not agent_text.strip()) or \
            (len(_at_lower) < 200 and any(p in _at_lower for p in _RATE_LIMIT_PHRASES)):
        result = RunResult(
            status="error",
            raw_stdout=claude_result.stdout[:50_000],
            raw_stderr=claude_result.stderr[:10_000],
            duration_seconds=round(duration, 1),
            error_message="usage_limit",
            session_id=accumulated_session_id,
        )
        _log_run(run_id, filename, file_score, model, result, tracker, harness_flags, target=target)
        return result

    if claude_result.result_subtype == "error_api_terminated":
        result = RunResult(
            status="error",
            raw_stdout=claude_result.stdout[:50_000],
            raw_stderr=claude_result.stderr[:10_000],
            duration_seconds=round(duration, 1),
            error_message="api_terminated",
            session_id=accumulated_session_id,
        )
        _log_run(run_id, filename, file_score, model, result, tracker, harness_flags, target=target)
        return result

    # ── Submit tool extraction + validation loop ───────────────────────────────
    last_validation_errors: list[dict] = []
    valid_payload: dict | None = None

    payload = _extract_submit_payload(all_tool_calls)
    if payload is not None:
        submit_attempt += 1
        _log_event("submit_attempt", run_id, filename, phase="audit", attempt=submit_attempt)
        vr = submit_mod.validate_audit_report(payload)
        if vr.ok:
            _log_event("submit_validation_passed", run_id, filename, phase="audit", attempt=submit_attempt)
            valid_payload = payload
        else:
            last_validation_errors = [vars(e) for e in vr.errors]
            _log_event(
                "submit_validation_failed", run_id, filename, phase="audit",
                attempt=submit_attempt, errors=last_validation_errors,
            )
            # Retry loop: return validation feedback to the agent
            while submit_attempt <= max_submit_retries and valid_payload is None:
                attempts_remaining = max_submit_retries - submit_attempt
                feedback = vr.to_feedback(submit_attempt, attempts_remaining)
                feedback_prompt = (
                    "Your submit_audit_report call failed validation. "
                    "Fix the errors below and call submit_audit_report again.\n\n"
                    + json.dumps(feedback, indent=2)
                )
                cr2, c2, it2, ot2, ev2, tc2, sid2 = _run_one_audit_session(
                    prompt=feedback_prompt,
                    model=model,
                    timeout=RUN_TIMEOUT_SEC,
                    target=target,
                    claude_home=claude_home,
                    resume_session_id=accumulated_session_id or None,
                )
                total_cost += c2
                total_in_tok += it2
                total_out_tok += ot2
                all_events.extend(ev2)
                all_tool_calls.extend(tc2)
                accumulated_session_id = sid2 or accumulated_session_id
                payload2 = _extract_submit_payload(tc2)
                if payload2 is None:
                    break
                submit_attempt += 1
                _log_event("submit_attempt", run_id, filename, phase="audit", attempt=submit_attempt)
                vr = submit_mod.validate_audit_report(payload2)
                if vr.ok:
                    _log_event("submit_validation_passed", run_id, filename, phase="audit", attempt=submit_attempt)
                    valid_payload = payload2
                else:
                    last_validation_errors = [vars(e) for e in vr.errors]
                    _log_event(
                        "submit_validation_failed", run_id, filename, phase="audit",
                        attempt=submit_attempt, errors=last_validation_errors,
                    )

    # ── Forced finalization turn (max_turns + 1) if no valid payload yet ───────
    if valid_payload is None:
        _log_event("forced_finalization_turn", run_id, filename, phase="audit")
        cr_final, cf, itf, otf, evf, tcf, sidf = _run_one_audit_session(
            prompt=FORCED_FINALIZATION_TEMPLATE.format(filename=filename),
            model=model,
            timeout=RUN_TIMEOUT_SEC,
            target=target,
            claude_home=claude_home,
            resume_session_id=accumulated_session_id or None,
        )
        total_cost += cf
        total_in_tok += itf
        total_out_tok += otf
        all_events.extend(evf)
        all_tool_calls.extend(tcf)
        accumulated_session_id = sidf or accumulated_session_id
        payload_final = _extract_submit_payload(tcf)
        if payload_final is not None:
            submit_attempt += 1
            _log_event("submit_attempt", run_id, filename, phase="audit", attempt=submit_attempt)
            vr_final = submit_mod.validate_audit_report(payload_final)
            if vr_final.ok:
                _log_event("submit_validation_passed", run_id, filename, phase="audit", attempt=submit_attempt)
                valid_payload = payload_final
            else:
                last_validation_errors = [vars(e) for e in vr_final.errors]
                _log_event(
                    "submit_validation_failed", run_id, filename, phase="audit",
                    attempt=submit_attempt, errors=last_validation_errors,
                )

    # ── Fallback if still nothing valid ───────────────────────────────────────
    if valid_payload is None:
        _log_event("fallback_emitted", run_id, filename, phase="audit")
        valid_payload = submit_mod.audit_fallback(
            attempt_count=submit_attempt,
            validation_errors=last_validation_errors,
        )
        fallback = True
    else:
        fallback = False

    # ── Map payload to RunResult ───────────────────────────────────────────────
    raw_status = valid_payload.get("status", "inconclusive")
    status = raw_status if raw_status in ("candidate", "no_finding", "inconclusive") else "inconclusive"

    # Save per-run transcript for all runs so agent output is never lost
    transcript_path = _save_transcript(run_id, filename, status, all_events, all_tool_calls,
                                        target_name=target.name)

    try:
        cumulative = tracker.record(total_cost)
    except budget_mod.BudgetExceededError:
        cumulative = tracker.spent()

    result = RunResult(
        status=status,
        summary=valid_payload.get("summary", ""),
        confidence=valid_payload.get("confidence", 0.0),
        findings=valid_payload.get("findings", []),
        negative_findings=valid_payload.get("negative_findings", []),
        diagnostic_trigger=valid_payload.get("diagnostic_trigger", ""),
        raw_stdout=claude_result.stdout[:50_000],
        raw_stderr=claude_result.stderr[:10_000],
        input_tokens=total_in_tok,
        output_tokens=total_out_tok,
        cost_usd=total_cost,
        duration_seconds=round(duration, 1),
        model=model,
        transcript_events=all_events,
        submit_attempts=submit_attempt,
        fallback=fallback,
    )

    _log_run(
        run_id, filename, file_score, model, result, tracker, harness_flags,
        target=target,
        transcript_path=str(transcript_path) if transcript_path else None,
        tool_call_count=len(all_tool_calls),
        retry_number=retry_number,
    )
    return result


def _log_event(
    event: str,
    run_id: str,
    target_file: str,
    phase: str = "audit",
    attempt: int | None = None,
    errors: list | None = None,
    session_id: str = "",
) -> None:
    """Emit a structured observability event to stdout."""
    parts = [f"[{event}] phase={phase} run={run_id[:8]} file={target_file}"]
    if attempt is not None:
        parts.append(f"attempt={attempt}")
    if errors:
        parts.append(f"errors={len(errors)}")
    print(" ".join(parts))


def _log_run(
    run_id: str,
    target_file: str,
    file_score: int,
    model: str,
    result: RunResult,
    tracker: budget_mod.BudgetTracker,
    harness_flags: list[str],
    target: TargetConfig | None = None,
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
        "prompt_version": "audit_v2",
        "target_file": target_file,
        "repo_revision": target.repo_revision if target else "",
        "container_image": target.container_image if target else "",
        "harness_flags": harness_flags,
        "file_score": file_score,
        "status": result.status,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_usd": result.cost_usd,
        "cumulative_cost_usd": tracker.spent(),
        "duration_seconds": result.duration_seconds,
        "tool_call_count": tool_call_count,
        "transcript_path": transcript_path,
        "retry_number": retry_number,
        "submit_attempts": result.submit_attempts,
        "fallback": result.fallback,
        "validation_verdict": None,
        "asan_triggered": None,
        "summary": result.summary or None,
        "confidence": result.confidence,
        "findings_count": len(result.findings),
        "diagnostic_trigger": result.diagnostic_trigger or None,
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
