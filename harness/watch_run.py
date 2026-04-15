"""Live pretty-printer for audit.jsonl — run in a second terminal alongside the pipeline.

Usage:  python3 -u watch_run.py
        python3 -u watch_run.py --tail   (follow new events as they land)
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import config

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


def dump_all() -> None:
    log = config.AUDIT_LOG
    if not log.exists():
        print("No audit.jsonl found yet.")
        return
    with open(log) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    print(_fmt(json.loads(line)))
                except Exception:
                    pass


def tail() -> None:
    log = config.AUDIT_LOG
    print(f"Watching {log} — Ctrl-C to stop\n")
    with open(log, "a+") as f:
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
    args = parser.parse_args()
    dump_all()
    if args.tail:
        tail()
