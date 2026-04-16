"""Live pretty-printer for audit.jsonl — run in a second terminal alongside the pipeline.

Usage:  python3 -u watch_run.py
        python3 -u watch_run.py --tail              (follow new events live)
        python3 -u watch_run.py --target miniupnpd  (per-target log)
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import config, load_target

COLORS = {
    "score":       "\033[90m",     # grey
    "audit_run":   "\033[94m",     # blue
    "gate_b":      "\033[93m",     # yellow
    "gate_a":      "\033[96m",     # cyan
    "confirmed":   "\033[92m",     # green
    "intractable": "\033[91m",     # red
    "error":       "\033[91m",     # red
}
RESET = "\033[0m"
BOLD  = "\033[1m"


def _strip_mcp_prefix(name: str) -> str:
    """'mcp__submit__submit_audit_report' → 'submit_audit_report'"""
    parts = name.split("__", 2)
    return parts[-1] if len(parts) == 3 and parts[0] == "mcp" else name


def _fmt(r: dict) -> str:
    event = r.get("event") or r.get("status", "?")
    ts    = r.get("timestamp", "")[-14:-5] if r.get("timestamp") else "         "
    file  = (r.get("target_file") or r.get("file") or r.get("focus_file") or "")
    file  = Path(file).name if file else ""
    cost  = r.get("cost_usd", 0)
    cumul = r.get("cumulative_cost_usd", 0)
    color = COLORS.get(event, "")

    extra = ""
    if event == "score":
        extra = f"score={r.get('score','?')}"
    elif event == "audit_run":
        status = r.get("status", "?")
        extra = f"status={status}  tools={r.get('tool_call_count','?')}  {r.get('error_message','')[:60]}"
    elif event == "gate_b":
        extra = f"verdict={r.get('judge_verdict','?')}"
    elif event == "gate_a":
        extra = f"asan={r.get('asan_triggered')}"
    elif event in ("confirmed", "intractable"):
        extra = BOLD + event.upper() + RESET

    return (
        f"{color}{ts}  {event:12}{RESET}  "
        f"{file:30}  ${cost:.4f}  cumul=${cumul:.4f}  {extra}"
    )


def dump_all(log: Path) -> None:
    if not log.exists():
        print(f"No audit log found at {log}")
        return
    with open(log) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    print(_fmt(json.loads(line)))
                except Exception:
                    pass


def tail(log: Path) -> None:
    if not log.exists():
        print(f"No audit log found at {log} — start the pipeline first.")
        return
    print(f"Watching {log} — Ctrl-C to stop\n")
    with open(log) as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                line = line.strip()
                if line:
                    try:
                        print(_fmt(json.loads(line)), flush=True)
                    except Exception:
                        pass
            else:
                time.sleep(0.5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tail", action="store_true", help="Follow new events live")
    parser.add_argument(
        "--target", default=None,
        help="Target name (default: auto-detected or MINIMYTHOS_TARGET env var)",
    )
    args = parser.parse_args()
    target = load_target(args.target)
    log = config.audit_log_path(target.name)
    dump_all(log)
    if args.tail:
        tail(log)
