"""Human-readable viewer for run transcripts, judge transcripts, and the audit log.

Usage:
    python3 show_run.py [--target NAME]                          # list all runs
    python3 show_run.py [--target NAME] <run_id_prefix>          # show audit transcript
    python3 show_run.py [--target NAME] --judge <run_id_prefix>  # show judge transcript
    python3 show_run.py [--target NAME] --log                    # dump raw audit.jsonl
"""

import json
import sys
from pathlib import Path

from config import config, load_target


def _read_all_records(audit_log: Path) -> list[dict]:
    """Read all records from audit.jsonl."""
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


def _strip_mcp_prefix(name: str) -> str:
    """'mcp__submit__submit_audit_report' → 'submit_audit_report'"""
    parts = name.split("__", 2)
    return parts[-1] if len(parts) == 3 and parts[0] == "mcp" else name


def list_runs(audit_log: Path) -> None:
    records = _read_all_records(audit_log)
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
          f"Total: ${total + judge_costs:.3f}  |  Runs: {len(audit_records)}"
          f"  |  Log: {audit_log}")


def _render_transcript(transcript_path: Path, label: str = "AUDIT", verbose: bool = False) -> None:
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
                        thinking = block.get("thinking", "")
                        if verbose:
                            print(f"\n[THINKING]\n{thinking}")
                        else:
                            print(f"\n[THINKING (truncated)]\n{thinking[:500]}...")
                    elif btype == "tool_use":
                        name = _strip_mcp_prefix(block.get("name", "?"))
                        inp = json.dumps(block.get("input", {}))
                        if not verbose:
                            inp = inp[:300]
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
                output = str(content) if verbose else str(content)[:500]
                print(f"\n{prefix}\n{output}")

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
                    inp = json.dumps(tc.get("input", {}))
                    if not verbose:
                        inp = inp[:100]
                    print(f"  {i}. {_strip_mcp_prefix(tc.get('name','?'))}  input={inp}")

            elif etype not in ("system", "user", "raw_line"):
                print(f"\n[{etype.upper()}] {json.dumps(event)[:200]}")


def show_transcript(run_id_prefix: str, transcripts_dir: Path, verbose: bool = False) -> None:
    matches = list(transcripts_dir.glob(f"*{run_id_prefix}*.jsonl"))
    if not matches:
        print(f"No audit transcript found for prefix: {run_id_prefix}")
        return
    if len(matches) > 1:
        print(f"Multiple matches: {[m.name for m in matches]}")
        return
    _render_transcript(matches[0], label="AUDIT", verbose=verbose)


def show_judge_transcript(run_id_prefix: str, judge_transcripts_dir: Path, verbose: bool = False) -> None:
    matches = list(judge_transcripts_dir.glob(f"*{run_id_prefix}*.jsonl"))
    if not matches:
        print(f"No judge transcript found for prefix: {run_id_prefix}")
        return
    if len(matches) > 1:
        print(f"Multiple matches: {[m.name for m in matches]}")
        return
    _render_transcript(matches[0], label="JUDGE", verbose=verbose)


def dump_log(audit_log: Path) -> None:
    records = _read_all_records(audit_log)
    for r in records:
        print(json.dumps(r, indent=2))
        print()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="View audit runs, transcripts, and logs"
    )
    parser.add_argument(
        "--target", default=None,
        help="Target name (default: auto-detected or MINIMYTHOS_TARGET env var)",
    )
    parser.add_argument("--log", action="store_true", help="Dump raw audit.jsonl")
    parser.add_argument("--judge", metavar="RUN_ID", help="Show judge transcript")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full thinking blocks and tool I/O without truncation")
    parser.add_argument("run_id", nargs="?", help="Show audit transcript for this run_id prefix")
    pargs = parser.parse_args()

    target = load_target(pargs.target)
    audit_log = config.audit_log_path(target.name)
    target_runs_dir = config.target_runs_dir(target.name)
    transcripts_dir = target_runs_dir / "transcripts"
    judge_transcripts_dir = target_runs_dir / "judge_transcripts"

    if pargs.log:
        dump_log(audit_log)
    elif pargs.judge:
        show_judge_transcript(pargs.judge, judge_transcripts_dir, verbose=pargs.verbose)
    elif pargs.run_id:
        show_transcript(pargs.run_id, transcripts_dir, verbose=pargs.verbose)
    else:
        list_runs(audit_log)
