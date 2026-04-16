"""Gate B — independent judge agent.

The judge receives only the defect_report and diagnostic_trigger from the audit agent.
It has NO access to the original agent's transcript or reasoning — it investigates
independently using tools, then returns one of:

  CONFIRMED   — independently verified the finding
  INTRACTABLE — dead end; no realistic exploit path from this file
  ERROR       — judge execution failed or returned unparseable output
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import budget as budget_mod
from claude_client import invoke_claude
from config import config, TargetConfig


JUDGE_PROMPT_PATH = config.PROMPTS_DIR / "judge.txt"
JUDGE_MODEL = config.JUDGE_MODEL
JUDGE_TIMEOUT_SEC = config.JUDGE_TIMEOUT_SEC
JUDGE_MAX_TURNS = config.JUDGE_MAX_TURNS
JUDGE_MAX_BUDGET_USD = config.JUDGE_MAX_BUDGET_USD
JUDGE_TRANSCRIPTS_DIR = config.RUNS_DIR / "judge_transcripts"

FORMAT_CORRECTION_PROMPT = """\
You previously acted as a security judge and produced an output, but the output could not be parsed as valid JSON.

Your output was:
---
{raw_output}
---

Extract the verdict fields from your output and return ONLY a valid JSON object — no markdown fences, no commentary, no XML tags. Nothing before or after the JSON.

Required format for CONFIRMED:
{{"verdict": "CONFIRMED", "reasoning": "...", "verified_trigger": "..."}}

Required format for RETRY:
{{"verdict": "RETRY", "reasoning": "...", "fix_instructions": "..."}}

Required format for INTRACTABLE:
{{"verdict": "INTRACTABLE", "reasoning": "..."}}
"""



@dataclass
class JudgeResult:
    verdict: str              # "CONFIRMED", "RETRY", "INTRACTABLE", "ERROR"
    reasoning: str
    retry_handoff: str        # populated when verdict == "RETRY"
    verified_trigger: str     # populated when verdict == "CONFIRMED"; Gate A runs this
    cost_usd: float
    duration_seconds: float
    transcript_path: str


def _parse_json_verdict(text: str) -> tuple[str, str, str, str]:
    """Parse judge output. Tries strict JSON first, falls back to XML tag extraction.

    Returns (verdict, reasoning, retry_handoff/fix_instructions, verified_trigger).
    Falls back to ("ERROR", reason, "", "") if nothing parseable.
    """
    valid_verdicts = ("CONFIRMED", "RETRY", "INTRACTABLE", "ERROR")
    stripped = text.strip()

    # ── Pre-process: strip markdown fences if present ────────────────────────
    stripped = re.sub(r'^```(?:json)?\s*', '', stripped, flags=re.MULTILINE)
    stripped = re.sub(r'\s*```\s*$', '', stripped, flags=re.MULTILINE)
    stripped = stripped.strip()

    # ── Attempt 1: strict JSON (preferred) ──────────────────────────────────
    # Scan last-to-first so we pick the final copy when the agent duplicates output.
    for m in reversed(list(re.finditer(r'\{', stripped))):
        candidate = stripped[m.start():]
        try:
            data = json.loads(candidate)
            verdict = data.get("verdict", "").upper()
            reasoning = data.get("reasoning", "")
            retry_handoff = data.get("fix_instructions", "")
            verified_trigger = data.get("verified_trigger", "")
            if verdict in valid_verdicts:
                if verdict == "CONFIRMED" and not verified_trigger.strip().startswith("#!"):
                    verdict = "ERROR"
                    reasoning = f"verified_trigger_not_bash: trigger does not start with #!"
                    verified_trigger = ""
                return verdict, reasoning, retry_handoff, verified_trigger
        except json.JSONDecodeError:
            pass

    # ── Attempt 2: XML tag extraction (actual agent format) ──────────────────
    vt = re.search(r"<verdict>(.*?)</verdict>", stripped, re.DOTALL)
    rs = re.search(r"<reasoning>(.*?)</reasoning>", stripped, re.DOTALL)
    rh = re.search(r"<retry_handoff>(.*?)</retry_handoff>", stripped, re.DOTALL)
    # judge.txt uses fix_instructions as key but agent may use retry_handoff tag
    fi = re.search(r"<fix_instructions>(.*?)</fix_instructions>", stripped, re.DOTALL)
    vtr = re.search(r"<verified_trigger>(.*?)</verified_trigger>", stripped, re.DOTALL)

    verdict = vt.group(1).strip().upper() if vt else ""
    reasoning = rs.group(1).strip() if rs else ""
    retry_handoff = (rh or fi)
    retry_handoff = retry_handoff.group(1).strip() if retry_handoff else ""
    verified_trigger = vtr.group(1).strip() if vtr else ""

    if verdict in valid_verdicts:
        if verdict == "CONFIRMED" and not verified_trigger.strip().startswith("#!"):
            verdict = "ERROR"
            reasoning = "verified_trigger_not_bash: trigger does not start with #!"
            verified_trigger = ""
        return verdict, reasoning, retry_handoff, verified_trigger

    return "ERROR", f"json_parse_failure: no parseable verdict in {len(stripped)} chars", "", ""


def _format_correction(
    raw_output: str,
    model: str,
    tracker: budget_mod.BudgetTracker,
    claude_home: str | None = None,
) -> tuple[str, str, str, str] | None:
    """Single cheap non-agentic call to reformat unparseable judge output.

    Returns (verdict, reasoning, retry_handoff, verified_trigger) on success,
    or None if the correction call also fails to parse.
    """
    prompt = FORMAT_CORRECTION_PROMPT.format(raw_output=raw_output[:8000])
    result = invoke_claude(
        prompt=prompt,
        model=model,
        timeout=60,
        output_format="json",
        max_turns=1,
        use_docker=False,
        claude_home=claude_home,
        verbose=False,
    )
    if result.error or not result.full_text.strip():
        return None
    try:
        tracker.record(result.cost_usd)
    except budget_mod.BudgetExceededError:
        pass
    verdict, reasoning, retry_handoff, verified_trigger = _parse_json_verdict(result.full_text)
    if verdict == "ERROR":
        return None
    return verdict, reasoning, retry_handoff, verified_trigger


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
    It uses tools to investigate, then returns CONFIRMED / RETRY / INTRACTABLE / ERROR.
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

    # Use centralized claude_client
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
    )

    duration = time.time() - start

    # Handle errors (timeout, exception)
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

    # Extract parsed fields
    events = claude_result.events or []
    full_text = claude_result.full_text
    cost_usd = claude_result.cost_usd
    in_tok = claude_result.input_tokens
    out_tok = claude_result.output_tokens

    # Detect usage/rate-limit: either zero tokens (clean rejection) or the
    # API injected its limit message into the result text mid-session.
    _RATE_LIMIT_PHRASES = ("you've hit your limit", "you have hit your limit", "rate limit")
    _ft_lower = full_text.strip().lower()
    if (in_tok == 0 and out_tok == 0 and cost_usd == 0.0 and not full_text.strip()) or \
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

    # Parse JSON verdict — with a cheap format-correction fallback
    verdict, reasoning, retry_handoff, verified_trigger = _parse_json_verdict(full_text)
    if verdict == "ERROR" and full_text.strip():
        corrected = _format_correction(
            raw_output=full_text,
            model=model,
            tracker=tracker,
            claude_home=claude_home,
        )
        if corrected is not None:
            verdict, reasoning, retry_handoff, verified_trigger = corrected

    # Save transcript — isolated from audit agent transcripts
    JUDGE_TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = JUDGE_TRANSCRIPTS_DIR / f"judge__{focus_file}__{verdict.lower()}__{run_id[:8]}.jsonl"
    with open(transcript_path, "w") as f:
        f.write(json.dumps({
            "type": "header", "run_id": run_id, "role": "judge", "focus_file": focus_file,
        }) + "\n")
        for event in events:
            f.write(json.dumps(event) + "\n")

    try:
        tracker.record(cost_usd)
    except budget_mod.BudgetExceededError:
        pass

    result = JudgeResult(
        verdict=verdict,
        reasoning=reasoning,
        retry_handoff=retry_handoff,
        verified_trigger=verified_trigger,
        cost_usd=cost_usd,
        duration_seconds=round(duration, 1),
        transcript_path=str(transcript_path),
    )
    _log_judge(run_id, result, focus_file, target=target, in_tok=in_tok, out_tok=out_tok)
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
        "retry_handoff": result.retry_handoff[:2000] if result.retry_handoff else None,
        "focus_file": focus_file,
        "cost_usd": result.cost_usd,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "duration_seconds": result.duration_seconds,
        "judge_transcript_path": result.transcript_path,
    }

    audit_log = config.audit_log_path(target.name) if target else config.AUDIT_LOG
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_log, "a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
