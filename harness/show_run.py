"""Human-readable viewer for run transcripts, judge transcripts, and the audit log.

Usage:
    python3 show_run.py                          # list all runs
    python3 show_run.py <run_id_prefix>          # show audit transcript for that run
    python3 show_run.py --judge <run_id_prefix>  # show judge transcript for that run
    python3 show_run.py --log                    # dump the raw audit.jsonl
"""

import json
import sys
from pathlib import Path

from config import config


TRANSCRIPTS_DIR = config.RUNS_DIR / "transcripts"
JUDGE_TRANSCRIPTS_DIR = config.RUNS_DIR / "judge_transcripts"


def _read_all_records() -> list[dict]:
    """Read all records from audit.jsonl."""
    audit_log = config.AUDIT_LOG
    if not audit_log.exists():
        return []
    records = []
    with open(audit_log) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def list_runs() -> None:
    records = _read_all_records()
    audit_records = [r for r in records if r.get("run_id") and r.get("status")]
    if not audit_records:
        print("No runs recorded yet.")
        return

    # Collect judge verdicts keyed by run_id
    judge_verdicts = {
        r["run_id"]: r.get("judge_verdict", "")
        for r in records
        if r.get("event") == "gate_b" and r.get("run_id")
    }

    print(f"{'run_id':38} {'file':28} {'status':18} {'judge':12} {'cost':8} {'tools':5} {'retry'}")
    print("-" * 118)
    for r in audit_records:
        rid = r.get("run_id", "?")[:36]
        f = Path(r.get("target_file", "?")).name[:26]
        status = r.get("status", "?")[:16]
        judge = judge_verdicts.get(r.get("run_id", ""), "")[:10]
        cost = f"${r.get('cost_usd', 0):.3f}"
        tools = str(r.get("tool_call_count", "?"))
        retry = f"#{r.get('retry_number', 0)}" if r.get("retry_number") else ""
        print(f"{rid:38} {f:28} {status:18} {judge:12} {cost:8} {tools:5} {retry}")

    total = sum(r.get("cost_usd", 0) for r in audit_records)
    judge_costs = sum(
        r.get("cost_usd", 0) for r in records if r.get("event") == "gate_b"
    )
    print(f"\nAudit spend: ${total:.3f}  Judge spend: ${judge_costs:.3f}  "
          f"Total: ${total + judge_costs:.3f}  |  Runs: {len(audit_records)}")


def _render_transcript(transcript_path: Path, label: str = "AUDIT") -> None:
    print(f"[{label} TRANSCRIPT] {transcript_path.name}\n{'='*80}")
    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"[unparseable] {line[:200]}")
                continue

            etype = event.get("type", "unknown")

            if etype == "header":
                role = event.get("role", label.lower())
                print(f"\n[{role.upper()}] run_id={event.get('run_id')}  "
                      f"file={event.get('target_file') or event.get('focus_file')}")

            elif etype == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    btype = block.get("type", "")
                    if btype == "text":
                        print(f"\n[ASSISTANT TEXT]\n{block.get('text', '')}")
                    elif btype == "thinking":
                        thinking = block.get("thinking", "")[:500]
                        print(f"\n[THINKING (truncated)]\n{thinking}...")
                    elif btype == "tool_use":
                        name = block.get("name", "?")
                        inp = json.dumps(block.get("input", {}))[:300]
                        print(f"\n[TOOL CALL] {name}\n  input: {inp}")

            elif etype == "tool_result":
                content = event.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                is_error = event.get("is_error", False)
                prefix = "[TOOL ERROR]" if is_error else "[TOOL RESULT]"
                print(f"\n{prefix}\n{str(content)[:500]}")

            elif etype == "result":
                cost = event.get("total_cost_usd", 0)
                usage = event.get("usage", {}) or {}
                print(f"\n[RESULT] cost=${cost:.4f}  "
                      f"in={usage.get('input_tokens',0)}  "
                      f"out={usage.get('output_tokens',0)}")

            elif etype == "tool_call_summary":
                calls = event.get("tool_calls", [])
                print(f"\n[TOOL SUMMARY] {len(calls)} tool calls:")
                for i, tc in enumerate(calls, 1):
                    inp = json.dumps(tc.get("input", {}))[:100]
                    print(f"  {i}. {tc.get('name','?')}  input={inp}")

            elif etype not in ("system", "user", "raw_line"):
                print(f"\n[{etype.upper()}] {json.dumps(event)[:200]}")


def show_transcript(run_id_prefix: str) -> None:
    matches = list(TRANSCRIPTS_DIR.glob(f"*{run_id_prefix}*.jsonl"))
    if not matches:
        print(f"No audit transcript found for prefix: {run_id_prefix}")
        return
    if len(matches) > 1:
        print(f"Multiple matches: {[m.name for m in matches]}")
        return
    _render_transcript(matches[0], label="AUDIT")


def show_judge_transcript(run_id_prefix: str) -> None:
    matches = list(JUDGE_TRANSCRIPTS_DIR.glob(f"*{run_id_prefix}*.jsonl"))
    if not matches:
        print(f"No judge transcript found for prefix: {run_id_prefix}")
        return
    if len(matches) > 1:
        print(f"Multiple matches: {[m.name for m in matches]}")
        return
    _render_transcript(matches[0], label="JUDGE")


def dump_log() -> None:
    records = _read_all_records()
    for r in records:
        print(json.dumps(r, indent=2))
        print()


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        list_runs()
    elif args[0] == "--log":
        dump_log()
    elif args[0] == "--judge":
        if len(args) < 2:
            print("Usage: show_run.py --judge <run_id_prefix>")
        else:
            show_judge_transcript(args[1])
    else:
        show_transcript(args[0])
