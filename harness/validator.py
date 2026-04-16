"""Gate B — independent judge agent.

The judge receives the audit findings from the audit agent.
It has NO access to the original agent's transcript or reasoning — it investigates
independently using tools, then finalises via submit_judge_verdict, returning one of:

  CONFIRMED   — independently verified the finding
  RETRY       — finding has flaws but the vulnerability might exist
  INTRACTABLE — dead end; no realistic exploit path from this file
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import budget as budget_mod
from claude_client import invoke_claude
from config import config, TargetConfig
import submit_tools as submit_mod


JUDGE_PROMPT_PATH = config.PROMPTS_DIR / "judge.txt"
JUDGE_MODEL = config.JUDGE_MODEL
JUDGE_TIMEOUT_SEC = config.JUDGE_TIMEOUT_SEC
JUDGE_MAX_TURNS = config.JUDGE_MAX_TURNS
JUDGE_MAX_BUDGET_USD = config.JUDGE_MAX_BUDGET_USD
JUDGE_TRANSCRIPTS_DIR = config.RUNS_DIR / "judge_transcripts"

FORCED_FINALIZATION_PROMPT = (
    "Finalization turn. Stop investigation now. "
    "Do not run additional exploratory actions. "
    "Call submit_judge_verdict immediately with your best final payload."
)



@dataclass
class JudgeResult:
    verdict: str              # "CONFIRMED", "RETRY", "INTRACTABLE", "ERROR"
    reasoning: str
    retry_handoff: str        # populated when verdict == "RETRY" (fix_instructions)
    verified_trigger: str     # populated when verdict == "CONFIRMED"; Gate A runs this
    cost_usd: float
    duration_seconds: float
    transcript_path: str
    checks: list = field(default_factory=list)
    confidence: float = 0.0
    submit_attempts: int = 0
    fallback: bool = False


_SUBMIT_JUDGE_TOOL_NAMES = {
    "submit_judge_verdict",
    submit_mod.submit_tool_name("submit_judge_verdict"),
}


def _extract_submit_payload(tool_calls: list[dict]) -> dict | None:
    """Return the input dict of the last submit_judge_verdict tool call, or None.

    Matches both the bare name and the MCP-namespaced name
    (``mcp__submit__submit_judge_verdict``).
    """
    for tc in reversed(tool_calls):
        if tc.get("name") in _SUBMIT_JUDGE_TOOL_NAMES:
            payload = tc.get("input")
            if isinstance(payload, dict):
                return payload
    return None


def _run_one_judge_session(
    prompt: str,
    model: str,
    target: TargetConfig,
    claude_home: str | None,
) -> tuple[object, float, int, int, list, list]:
    """Invoke claude once and return (claude_result, cost, in_tok, out_tok, events, tool_calls)."""
    claude_result = invoke_claude(
        prompt=prompt,
        model=model,
        timeout=JUDGE_TIMEOUT_SEC,
        output_format="stream-json",
        max_turns=JUDGE_MAX_TURNS,
        max_budget_usd=JUDGE_MAX_BUDGET_USD,
        use_docker=True,
        container_name=target.container_name,
        container_workdir=target.container_workdir,
        container_home=config.CONTAINER_HOME,
        claude_home=claude_home,
        verbose=True,
        mcp_config=submit_mod.build_submit_mcp_config(config.CONTAINER_MCP_SERVER_PATH),
    )
    return (
        claude_result,
        claude_result.cost_usd,
        claude_result.input_tokens,
        claude_result.output_tokens,
        claude_result.events or [],
        claude_result.tool_calls or [],
    )


def _log_event(
    event: str,
    run_id: str,
    target_file: str,
    phase: str = "judge",
    attempt: int | None = None,
    errors: list | None = None,
) -> None:
    """Emit a structured observability event to stdout."""
    parts = [f"[{event}] phase={phase} run={run_id[:8]} file={target_file}"]
    if attempt is not None:
        parts.append(f"attempt={attempt}")
    if errors:
        parts.append(f"errors={len(errors)}")
    print(" ".join(parts))


def judge(
    defect_report: str,
    diagnostic_trigger: str,
    focus_file: str,
    source_dir: str | Path,
    container_name: str,
    run_id: str,
    tracker: budget_mod.BudgetTracker,
    target: TargetConfig,
    model: str = JUDGE_MODEL,
    claude_home: str | None = None,
) -> JudgeResult:
    """
    Run the independent judge agent against a candidate finding.

    The judge sees only the claim and trigger — not the original agent's work.
    It uses tools to investigate, then finalises via submit_judge_verdict.
    """
    template = JUDGE_PROMPT_PATH.read_text()
    prompt = (
        template
        .replace("{defect_report}", defect_report)
        .replace("{diagnostic_trigger}", diagnostic_trigger)
        .replace("{focus_file}", focus_file)
        .replace("{source_dir}", target.container_workdir)
    )

    start = time.time()
    total_cost = 0.0
    total_in_tok = 0
    total_out_tok = 0
    all_events: list = []
    all_tool_calls: list = []
    submit_attempt = 0
    max_submit_retries = config.SUBMIT_MAX_RETRIES

    # ── Main session ─────────────────────────────────────────────────────────
    claude_result, cost, in_tok, out_tok, events, tool_calls = _run_one_judge_session(
        prompt=prompt,
        model=model,
        target=target,
        claude_home=claude_home,
    )
    total_cost += cost
    total_in_tok += in_tok
    total_out_tok += out_tok
    all_events.extend(events)
    all_tool_calls.extend(tool_calls)

    duration = time.time() - start

    # ── Hard-error checks ────────────────────────────────────────────────
    if claude_result.error:
        result = JudgeResult(
            verdict="ERROR",
            reasoning=claude_result.error,
            retry_handoff="",
            verified_trigger="",
            cost_usd=0.0,
            duration_seconds=round(duration, 1),
            transcript_path="",
        )
        _log_judge(run_id, result, focus_file, target=target)
        return result

    full_text = claude_result.full_text
    _RATE_LIMIT_PHRASES = ("you've hit your limit", "you have hit your limit", "rate limit")
    _ft_lower = full_text.strip().lower()
    if (in_tok == 0 and out_tok == 0 and cost == 0.0 and not full_text.strip()) or \
            (len(_ft_lower) < 200 and any(p in _ft_lower for p in _RATE_LIMIT_PHRASES)):
        result = JudgeResult(
            verdict="ERROR",
            reasoning="usage_limit",
            retry_handoff="",
            verified_trigger="",
            cost_usd=0.0,
            duration_seconds=round(duration, 1),
            transcript_path="",
        )
        _log_judge(run_id, result, focus_file, target=target)
        return result

    # ── Submit tool extraction + validation loop ───────────────────────────────
    last_validation_errors: list[dict] = []
    valid_payload: dict | None = None

    payload = _extract_submit_payload(all_tool_calls)
    if payload is not None:
        submit_attempt += 1
        _log_event("submit_attempt", run_id, focus_file, phase="judge", attempt=submit_attempt)
        vr = submit_mod.validate_judge_verdict(payload)
        if vr.ok:
            _log_event("submit_validation_passed", run_id, focus_file, phase="judge", attempt=submit_attempt)
            valid_payload = payload
        else:
            last_validation_errors = [vars(e) for e in vr.errors]
            _log_event(
                "submit_validation_failed", run_id, focus_file, phase="judge",
                attempt=submit_attempt, errors=last_validation_errors,
            )
            while submit_attempt <= max_submit_retries and valid_payload is None:
                attempts_remaining = max_submit_retries - submit_attempt
                feedback = vr.to_feedback(submit_attempt, attempts_remaining)
                feedback_prompt = (
                    "Your submit_judge_verdict call failed validation. "
                    "Fix the errors below and call submit_judge_verdict again.\n\n"
                    + json.dumps(feedback, indent=2)
                )
                cr2, c2, it2, ot2, ev2, tc2 = _run_one_judge_session(
                    prompt=feedback_prompt,
                    model=model,
                    target=target,
                    claude_home=claude_home,
                )
                total_cost += c2
                total_in_tok += it2
                total_out_tok += ot2
                all_events.extend(ev2)
                all_tool_calls.extend(tc2)
                payload2 = _extract_submit_payload(tc2)
                if payload2 is None:
                    break
                submit_attempt += 1
                _log_event("submit_attempt", run_id, focus_file, phase="judge", attempt=submit_attempt)
                vr = submit_mod.validate_judge_verdict(payload2)
                if vr.ok:
                    _log_event("submit_validation_passed", run_id, focus_file, phase="judge", attempt=submit_attempt)
                    valid_payload = payload2
                else:
                    last_validation_errors = [vars(e) for e in vr.errors]
                    _log_event(
                        "submit_validation_failed", run_id, focus_file, phase="judge",
                        attempt=submit_attempt, errors=last_validation_errors,
                    )

    # ── Forced finalization turn ───────────────────────────────────────────
    if valid_payload is None:
        _log_event("forced_finalization_turn", run_id, focus_file, phase="judge")
        cr_final, cf, itf, otf, evf, tcf = _run_one_judge_session(
            prompt=FORCED_FINALIZATION_PROMPT,
            model=model,
            target=target,
            claude_home=claude_home,
        )
        total_cost += cf
        total_in_tok += itf
        total_out_tok += otf
        all_events.extend(evf)
        all_tool_calls.extend(tcf)
        payload_final = _extract_submit_payload(tcf)
        if payload_final is not None:
            submit_attempt += 1
            _log_event("submit_attempt", run_id, focus_file, phase="judge", attempt=submit_attempt)
            vr_final = submit_mod.validate_judge_verdict(payload_final)
            if vr_final.ok:
                _log_event("submit_validation_passed", run_id, focus_file, phase="judge", attempt=submit_attempt)
                valid_payload = payload_final
            else:
                last_validation_errors = [vars(e) for e in vr_final.errors]
                _log_event(
                    "submit_validation_failed", run_id, focus_file, phase="judge",
                    attempt=submit_attempt, errors=last_validation_errors,
                )

    # ── Fallback ─────────────────────────────────────────────────────────────
    if valid_payload is None:
        _log_event("fallback_emitted", run_id, focus_file, phase="judge")
        valid_payload = submit_mod.judge_fallback()
        fallback = True
    else:
        fallback = False

    # ── Map payload to JudgeResult ──────────────────────────────────────────
    verdict = valid_payload.get("verdict", "RETRY")
    if verdict not in ("CONFIRMED", "RETRY", "INTRACTABLE"):
        verdict = "RETRY"

    # Save transcript
    JUDGE_TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = JUDGE_TRANSCRIPTS_DIR / f"judge__{focus_file}__{verdict.lower()}__{run_id[:8]}.jsonl"
    with open(transcript_path, "w") as f:
        f.write(json.dumps({
            "type": "header", "run_id": run_id, "role": "judge", "focus_file": focus_file,
        }) + "\n")
        for event in all_events:
            f.write(json.dumps(event) + "\n")

    try:
        tracker.record(total_cost)
    except budget_mod.BudgetExceededError:
        pass

    result = JudgeResult(
        verdict=verdict,
        reasoning=valid_payload.get("reasoning", ""),
        retry_handoff=valid_payload.get("fix_instructions", ""),
        verified_trigger=valid_payload.get("verified_trigger", ""),
        cost_usd=total_cost,
        duration_seconds=round(duration, 1),
        transcript_path=str(transcript_path),
        checks=valid_payload.get("checks", []),
        confidence=valid_payload.get("confidence", 0.0),
        submit_attempts=submit_attempt,
        fallback=fallback,
    )
    _log_judge(run_id, result, focus_file, target=target, in_tok=total_in_tok, out_tok=total_out_tok)
    return result


def _log_judge(
    run_id: str,
    result: JudgeResult,
    focus_file: str,
    target: TargetConfig | None = None,
    in_tok: int = 0,
    out_tok: int = 0,
) -> None:
    """Append judge record to per-target audit.jsonl."""
    from datetime import datetime, timezone

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "event": "gate_b",
        "judge_verdict": result.verdict,
        "judge_reasoning": result.reasoning[:2000] if result.reasoning else None,
        "judge_confidence": result.confidence,
        "retry_handoff": result.retry_handoff[:2000] if result.retry_handoff else None,
        "focus_file": focus_file,
        "cost_usd": result.cost_usd,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "duration_seconds": result.duration_seconds,
        "judge_transcript_path": result.transcript_path,
        "submit_attempts": result.submit_attempts,
        "fallback": result.fallback,
    }

    audit_log = config.audit_log_path(target.name) if target else config.AUDIT_LOG
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_log, "a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
