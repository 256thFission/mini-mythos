"""File scoring pass — assigns each .c/.h file a risk score 1–5 via LLM.

Uses `claude -p` CLI for all LLM calls (consistent with runner.py, no separate API key needed).
Score results are persisted to runs/scores.json so re-runs skip already-scored files.
"""

import json
import re
from pathlib import Path

import budget as budget_mod
from claude_client import invoke_claude
from config import config, TargetConfig


SCORE_PROMPT_PATH = config.PROMPTS_DIR / "score.txt"
SCORE_MODEL = config.SCORE_MODEL
MAX_FILE_BYTES = config.MAX_FILE_BYTES
DEFAULT_SCORE = config.DEFAULT_SCORE
SCORE_TIMEOUT_SEC = config.SCORE_TIMEOUT_SEC


def load_cached_scores(target: TargetConfig) -> dict[str, int]:
    """Return {basename: score} from the per-target scores.json."""
    cache_path = config.score_cache_path(target.name)
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_score(basename: str, score: int, target: TargetConfig) -> None:
    """Persist a single score to the per-target scores.json."""
    cache = load_cached_scores(target)
    cache[basename] = score
    cache_path = config.score_cache_path(target.name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2) + "\n")


def _load_prompt_template() -> str:
    return SCORE_PROMPT_PATH.read_text()


def _log_score(
    filepath: Path,
    score: int,
    cost_usd: float,
    in_tok: int,
    out_tok: int,
    tracker: budget_mod.BudgetTracker,
    target: TargetConfig,
) -> None:
    """Append score record to per-target audit.jsonl."""
    from datetime import datetime, timezone

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "score",
        "file": str(filepath),
        "score": score,
        "model": SCORE_MODEL,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd": cost_usd,
        "cumulative_cost_usd": tracker.spent(),
    }

    audit_log = config.audit_log_path(target.name)
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_log, "a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()


def score_file(
    filepath: str | Path,
    tracker: budget_mod.BudgetTracker,
    target: TargetConfig,
    claude_home: str | None = None,
) -> int:
    """Return a 1–5 risk score for the given C source file."""
    filepath = Path(filepath)

    try:
        contents = filepath.read_text(errors="replace")
    except OSError as e:
        print(f"[scorer] WARNING: cannot read {filepath}: {e}")
        return DEFAULT_SCORE

    if len(contents) > MAX_FILE_BYTES:
        contents = contents[:MAX_FILE_BYTES] + "\n... [truncated]"

    template = _load_prompt_template()
    prompt = template.replace("{filename}", filepath.name).replace(
        "{file_contents}", contents
    )

    # Use centralized claude_client (no docker for scoring - runs locally)
    claude_result = invoke_claude(
        prompt=prompt,
        model=SCORE_MODEL,
        timeout=SCORE_TIMEOUT_SEC,
        output_format="json",
        use_docker=False,
        claude_home=claude_home,
    )

    # Handle errors
    if claude_result.error:
        print(f"[scorer] WARNING: error scoring {filepath.name}: {claude_result.error}")
        return DEFAULT_SCORE

    cost_usd = claude_result.cost_usd
    raw_text = claude_result.full_text
    in_tok = claude_result.input_tokens
    out_tok = claude_result.output_tokens

    try:
        cumulative = tracker.record(cost_usd)
    except budget_mod.BudgetExceededError:
        cumulative = tracker.spent()

    # Guard: if no tokens were consumed, claude CLI didn't actually run inference.
    # If cost is also zero and text is empty, this is a usage limit — signal the
    # caller to stop by returning -1.
    if in_tok == 0 and out_tok == 0:
        if cost_usd == 0.0 and not raw_text.strip():
            print(f"[scorer] HALTING — usage limit reached for {filepath.name!r}")
            return -1
        print(f"[scorer] WARNING: no inference for {filepath.name!r} (auth failure?): {raw_text.strip()!r}")
        return DEFAULT_SCORE

    # Parse score from response (look for 1-5)
    match = re.search(r"[1-5]", raw_text.strip())
    score = int(match.group()) if match else DEFAULT_SCORE
    if not match:
        print(f"[scorer] WARNING: unexpected score for {filepath.name!r}: {raw_text.strip()!r}")

    _log_score(filepath, score, cost_usd, in_tok, out_tok, tracker, target=target)
    _save_score(filepath.name, score, target=target)
    return score


def _load_reachable_symbols(target: TargetConfig) -> dict[str, list[str]] | None:
    """Load per-target reachable_symbols.json if it exists. Returns {filename: [symbols]} or None."""
    path = config.reachable_symbols_path(target.name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def score_directory(
    source_dir: str | Path,
    tracker: budget_mod.BudgetTracker,
    target: TargetConfig,
    claude_home: str | None = None,
) -> list[tuple[Path, int]]:
    """Score all .c and .h files. Returns list sorted by score descending.

    Files already present in the per-target scores.json are skipped (cache hit).
    Files with zero exported symbols in reachable_symbols.json are skipped (dead code).
    """
    source_dir = Path(source_dir)

    files = sorted(
        list(source_dir.glob("*.c")) + list(source_dir.glob("*.h"))
    )

    # Filter out dead files using per-target reachable_symbols.json
    symbols = _load_reachable_symbols(target)
    dead_files: set[str] = set()
    if symbols is None:
        expected_path = config.reachable_symbols_path(target.name)
        print(
            f"[scorer] WARNING: reachable_symbols.json not found at {expected_path}\n"
            f"  Dead-code filtering is DISABLED — all {len(files)} files will be scored.\n"
            f"  To enable it, copy the file from the container after building:\n"
            f"    mkdir -p {expected_path.parent}\n"
            f"    docker cp {target.container_name}:{target.container_workdir}/reachable_symbols.json"
            f" {expected_path}"
        )
    else:
        for fname, syms in symbols.items():
            if len(syms) == 0:
                dead_files.add(fname)
        if dead_files:
            print(f"[scorer] Skipping {len(dead_files)} dead-code file(s): {sorted(dead_files)}")
            files = [f for f in files if f.name not in dead_files]

    cache = load_cached_scores(target)
    to_score = [f for f in files if f.name not in cache]
    cached_count = len(files) - len(to_score)

    print(f"[scorer] {len(files)} files in {source_dir}")
    print(f"[scorer]   {cached_count} cached, {len(to_score)} to score")

    scored = []

    # Carry over cached scores
    for f in files:
        if f.name in cache:
            scored.append((f, cache[f.name]))

    # Score new files
    for f in to_score:
        if not tracker.can_dispatch(estimated_cost=0.01):
            print("[scorer] Budget too low to continue scoring — stopping early")
            break
        score = score_file(f, tracker=tracker, target=target, claude_home=claude_home)
        if score == -1:
            print("[scorer] Usage limit reached — stopping scoring early")
            break
        print(f"[scorer]   {f.name}: {score}")
        scored.append((f, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored
