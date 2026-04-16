"""Main orchestrator: score → dispatch → validate (Gate B) → verify (Gate A) → log.

Usage:
    python3 orchestrator.py [--target NAME] [--source-dir PATH] [--model MODEL]
                            [--budget USD] [--max-runs N] [--dry-run]

The target is loaded from targets/<name>/target.toml. Source dir defaults to
the container_workdir from the target spec.
"""

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import budget as budget_mod
from claude_client import invoke_claude
from config import config, TargetConfig, load_target
import scorer as scorer_mod
import runner as runner_mod
import validator as validator_mod
import verifier as verifier_mod


def _ensure_source_dir(target: TargetConfig, override: str | None) -> Path:
    """Resolve the host-side source directory for *target*.

    Resolution order:
      1. ``--source-dir`` CLI override (must exist)
      2. ``sources/<target>/`` local cache — cloned from repo_url at repo_revision
         if the directory doesn't exist yet, or if it exists but is at the wrong
         revision it is updated via ``git checkout``.
      3. Fallback to container_workdir (old behaviour, errors if path absent)
    """
    if override:
        p = Path(override)
        if not p.exists():
            print(f"[orchestrator] ERROR: --source-dir {p} does not exist.")
            sys.exit(1)
        return p

    if target.repo_url:
        cache_root = config.source_cache_dir(target.name)
        build_subdir = cache_root / target.build_dir if target.build_dir else cache_root

        if not cache_root.exists():
            print(f"[orchestrator] Cloning {target.repo_url} → {cache_root} ...")
            result = subprocess.run(
                ["git", "clone", "--quiet", target.repo_url, str(cache_root)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"[orchestrator] ERROR: git clone failed:\n{result.stderr}")
                sys.exit(1)

        if target.repo_revision:
            current = subprocess.run(
                ["git", "-C", str(cache_root), "rev-parse", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip()
            if not current.startswith(target.repo_revision) and not target.repo_revision.startswith(current):
                print(f"[orchestrator] Checking out {target.repo_revision[:12]} in {cache_root} ...")
                result = subprocess.run(
                    ["git", "-C", str(cache_root), "checkout", "--quiet", target.repo_revision],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    print(f"[orchestrator] ERROR: git checkout failed:\n{result.stderr}")
                    sys.exit(1)

        if not build_subdir.exists():
            print(f"[orchestrator] ERROR: build_dir '{target.build_dir}' not found inside cloned repo at {cache_root}.")
            sys.exit(1)
        return build_subdir

    p = Path(target.container_workdir)
    if not p.exists():
        print(f"[orchestrator] ERROR: source_dir {p} does not exist.")
        print("  → Add repo_url/repo_revision to target.toml, or pass --source-dir.")
        sys.exit(1)
    return p


def _load_skip_set(target: TargetConfig) -> set[str]:
    """Return filenames that have a confirmed or intractable event in the per-target audit.jsonl."""
    skip = set()
    audit_log = config.audit_log_path(target.name)
    if not audit_log.exists():
        return skip
    for line in audit_log.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = e.get("event") or e.get("status") or ""
        if event in ("confirmed", "intractable"):
            fname = e.get("target_file")
            if fname:
                skip.add(fname)
    return skip


def _load_interrupted_sessions(target: TargetConfig) -> dict[str, str]:
    """Return {filename: session_id} for files whose most recent audit run was api_terminated.

    Only files where the last logged entry has error_message='api_terminated' and a non-empty
    session_id are returned. Files with a later successful/intractable/confirmed entry are
    excluded automatically because iteration keeps only the last entry per file.
    """
    audit_log = config.audit_log_path(target.name)
    if not audit_log.exists():
        return {}
    latest: dict[str, dict] = {}
    for line in audit_log.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        fname = e.get("target_file")
        if fname:
            latest[fname] = e
    return {
        fname: entry["session_id"]
        for fname, entry in latest.items()
        if entry.get("error_message") == "api_terminated" and entry.get("session_id")
    }


def _create_artifact_dir(run_id: str, filepath: Path, file_score: int) -> Path:
    """Create artifact directory for a run and copy source file.

    Returns the path to the artifact directory.
    """
    artifact_dir = config.ARTIFACTS_DIR / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Copy source file
    shutil.copy(filepath, artifact_dir / filepath.name)

    # Write metadata
    metadata = {
        "run_id": run_id,
        "target_file": filepath.name,
        "initial_score": file_score,
        "models": {
            "audit": config.AUDIT_MODEL,
            "score": config.SCORE_MODEL,
            "judge": config.JUDGE_MODEL,
        },
        "start_time": datetime.now(timezone.utc).isoformat(),
        "budget_usd": config.PER_RUN_BUDGET_USD,
    }
    (artifact_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    return artifact_dir


def _append_audit_log(record: dict, target: TargetConfig) -> None:
    """Append a record to the per-target audit log."""
    audit_log = config.audit_log_path(target.name)
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_log, "a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()

def run_pipeline(
    target: TargetConfig,
    source_dir: str,
    model: str = config.AUDIT_MODEL,
    hard_budget: float = config.HARD_BUDGET_USD,
    max_runs: int | None = None,
    dry_run: bool = False,
    skip_docker: bool = False,
    claude_home: str | None = None,
) -> None:
    source_dir = Path(source_dir)
    if not source_dir.exists():
        print(f"[orchestrator] ERROR: source_dir {source_dir} does not exist.")
        print("  → Clone the repo first or set --source-dir to a valid path.")
        sys.exit(1)

    tracker = budget_mod.BudgetTracker(hard_limit=hard_budget, target_name=target.name)

    print(f"[orchestrator] Starting MiniMythos pipeline")
    print(f"  target     : {target.name} ({target.description})")
    print(f"  source_dir : {source_dir}")
    print(f"  model      : {model}")
    print(f"  budget     : ${hard_budget:.2f}")
    print(f"  container  : {target.container_name}")
    print(f"  dry_run    : {dry_run}")
    if skip_docker:
        print()
        print("  NOTE: --skip-docker is active.")
        print("    Gate A (trigger execution) will be SKIPPED.")
        print("    The audit agent and judge still run inside Docker via 'docker exec'.")
        print("    The container must be running for agents to work.")
    print()

    # ── Container health check ───────────────────────────────────────
    # NOTE: --skip-docker only skips Gate A (trigger execution).
    # Agents always run inside Docker, so we always need the container.
    if not dry_run:
        if not verifier_mod.container_is_running(target.container_name):
            print(f"[orchestrator] Container '{target.container_name}' not running — attempting start ...")
            ok = verifier_mod.start_container(image=target.container_image, name=target.container_name)
            if not ok:
                print(
                    "[orchestrator] ERROR: container could not be started.\n"
                    "  Audit agents run inside Docker — the container is required.\n"
                    f"  Build and start it first:\n"
                    f"    docker build -t {target.container_image} targets/{target.name}/\n"
                    f"    docker run -d --name {target.container_name} {target.container_image}"
                )
                sys.exit(1)

        # Copy claude auth into container so agents run natively without docker exec
        if claude_home:
            print(f"[orchestrator] Copying claude auth into container ...")
            verifier_mod.copy_claude_auth(
                target.container_name, claude_home, container_home=config.CONTAINER_HOME
            )

        # Copy the submit MCP server so agents can call submit_audit_report /
        # submit_judge_verdict as real (MCP-registered) tools.
        verifier_mod.copy_submit_mcp_server(
            target.container_name,
            container_home=config.CONTAINER_HOME,
            dest_path=config.CONTAINER_MCP_SERVER_PATH,
        )

        # ── Preflight: fast host auth check, then Docker probe ───────────────
        print("[orchestrator] Preflight: checking Claude API availability ...")

        # Step 1: host-side check (fast — catches 401/expired token in ~350ms)
        host_probe = invoke_claude(
            prompt="Reply with the single word PONG.",
            model=config.JUDGE_MODEL,
            timeout=15,
            output_format="json",
            use_docker=False,
            claude_home=claude_home,
            verbose=False,
        )
        if host_probe.error or host_probe.input_tokens == 0:
            reason = host_probe.error or host_probe.full_text.strip()[:120]
            print(f"[orchestrator] ABORT: Claude auth failed on host — {reason}")
            print("  Run `claude` interactively to refresh your OAuth token, then retry.")
            sys.exit(1)

        # Step 2: Docker probe (confirms container auth is also live)
        probe = invoke_claude(
            prompt="Reply with the single word PONG.",
            model=config.JUDGE_MODEL,
            timeout=45,
            output_format="json",
            use_docker=True,
            container_name=target.container_name,
            container_workdir=target.container_workdir,
            container_home=config.CONTAINER_HOME,
            claude_home=claude_home,
            verbose=False,
        )
        if probe.error or probe.input_tokens == 0:
            reason = probe.error or "zero tokens returned (rate-limited or not logged in)"
            print(f"[orchestrator] ABORT: Claude API unavailable in container — {reason}")
            print("  Wait for usage to reset, then rerun. No budget was spent.")
            sys.exit(1)
        print(f"[orchestrator] Preflight OK (cost=${probe.cost_usd:.4f})\n")

    # ── Phase 1: score all source files ────────────────────────────────
    print("[orchestrator] Phase 1: scoring source files ...")
    scored_files = scorer_mod.score_directory(
        source_dir, tracker=tracker, target=target, claude_home=claude_home
    )

    if not scored_files:
        print("[orchestrator] No files found to audit. Exiting.")
        return

    print(f"\n[orchestrator] File queue ({len(scored_files)} files):")
    for f, s in scored_files[:10]:
        print(f"  [{s}] {f.name}")
    if len(scored_files) > 10:
        print(f"  ... and {len(scored_files) - 10} more")
    print()

    # ── Phase 2: dispatch audit runs ────────────────────────────────
    print("[orchestrator] Phase 2: dispatching audit runs ...")
    skip_set = _load_skip_set(target)
    if skip_set:
        print(f"[orchestrator] Skipping {len(skip_set)} already-resolved file(s): {sorted(skip_set)}")
    interrupted_sessions = _load_interrupted_sessions(target)
    if interrupted_sessions:
        print(f"[orchestrator] Resumable interrupted session(s): {sorted(interrupted_sessions)}")
    runs_dispatched = 0
    confirmed = False
    halt = False  # set True to break outer loop without claiming a confirmed finding

    for filepath, score in scored_files:
        if confirmed:
            print("[orchestrator] Confirmed finding — halting dispatch.")
            break

        if halt:
            break

        if max_runs is not None and runs_dispatched >= max_runs:
            print(f"[orchestrator] Reached --max-runs {max_runs} — halting.")
            break

        if not tracker.can_dispatch(estimated_cost=runner_mod.PER_RUN_BUDGET_USD):
            remaining = tracker.remaining()
            print(
                f"[orchestrator] Budget exhausted — "
                f"${remaining:.2f} remaining < ${runner_mod.PER_RUN_BUDGET_USD:.2f} min. Halting."
            )
            break

        if filepath.name in skip_set:
            print(f"\n[orchestrator] Skipping {filepath.name} (confirmed/intractable)")
            continue

        print(f"\n[orchestrator] File: {filepath.name} (score={score})")

        if dry_run:
            print("  [dry-run] skipping actual claude invocation")
            runs_dispatched += 1
            continue

        # ── Per-file retry loop ────────────────────────────────
        retry_count = 0
        retry_handoff = None
        # Use saved session from a prior interrupted run (first attempt only).
        pending_resume_session_id = interrupted_sessions.pop(filepath.name, None)
        if pending_resume_session_id:
            print(f"  [resuming interrupted session {pending_resume_session_id[:8]}...]")

        while True:
            if not tracker.can_dispatch(estimated_cost=runner_mod.PER_RUN_BUDGET_USD):
                print("[orchestrator] Budget exhausted mid-retry — halting.")
                halt = True
                break

            run_id = str(uuid.uuid4())
            attempt_label = f"attempt {retry_count + 1}" if retry_count > 0 else "initial"
            print(
                f"  Run {runs_dispatched + 1} ({attempt_label}): "
                f"run_id={run_id[:8]}...  budget=${tracker.remaining():.2f}"
            )

            # Create artifact directory for this run (copies source file + writes metadata.json)
            _create_artifact_dir(run_id, filepath, score)

            # ── Audit agent ────────────────────────────────
            # Consume the saved session_id on the first attempt only.
            resume_session_id = pending_resume_session_id
            pending_resume_session_id = None
            result = runner_mod.run_audit(
                source_dir=source_dir,
                filename=filepath.name,
                file_score=score,
                run_id=run_id,
                tracker=tracker,
                target=target,
                model=model,
                claude_home=claude_home,
                retry_handoff=retry_handoff,
                retry_number=retry_count,
                resume_session_id=resume_session_id,
            )
            runs_dispatched += 1

            print(
                f"  status={result.status}  cost=${result.cost_usd:.3f}  "
                f"duration={result.duration_seconds:.0f}s"
            )

            if result.status == "error" and result.error_message == "usage_limit":
                print(
                    f"  [HALTING — usage limit reached. "
                    f"Wait for API usage to reset, then rerun.]"
                )
                halt = True
                break

            if result.status == "error" and result.error_message == "api_terminated":
                print(
                    f"  [HALTING — API terminated session unexpectedly. "
                    f"This is likely transient. Rerun to retry this file.]"
                )
                halt = True
                break

            if result.status in ("error", "declined"):
                print(f"  [skipping — {result.status}]")
                break

            if result.status == "no_finding":
                print(f"  [no finding: {result.summary[:80]}]")
                break

            if result.status != "candidate":
                break

            # ── Gate B: independent judge ──────────────────────────────
            # Build a defect_report string from the primary finding for the judge prompt.
            primary_finding = result.findings[0] if result.findings else {}
            defect_report_for_judge = (
                f"{primary_finding.get('title', '')}\n"
                f"File: {primary_finding.get('file', '')} "
                f"L{primary_finding.get('line_start', '')}–{primary_finding.get('line_end', '')}\n"
                f"{primary_finding.get('claim', '')}"
            ).strip() or result.summary
            print(f"  → Gate B: independent judge investigating ...")
            judge_result = validator_mod.judge(
                defect_report=defect_report_for_judge,
                diagnostic_trigger=result.diagnostic_trigger,
                focus_file=filepath.name,
                source_dir=source_dir,
                container_name=target.container_name,
                run_id=run_id,
                tracker=tracker,
                target=target,
                claude_home=claude_home,
            )
            print(
                f"  Judge verdict: {judge_result.verdict}  "
                f"confidence={judge_result.confidence:.2f}  "
                f"cost=${judge_result.cost_usd:.3f}  "
                f"duration={judge_result.duration_seconds:.0f}s"
            )

            if judge_result.verdict == "CONFIRMED":
                # ── Gate A: trigger execution ───────────────────────
                if skip_docker:
                    print("  [Gate A SKIPPED — --skip-docker flag is set]")
                    _append_audit_log({
                        "run_id": run_id,
                        "event": "gate_a_skipped",
                        "reason": "--skip-docker flag",
                    }, target=target)
                    break

                # Prefer the judge's verified trigger — it was actually run and confirmed.
                # Fall back to the agent's trigger only if the judge didn't provide one.
                trigger_to_run = judge_result.verified_trigger or result.diagnostic_trigger
                trigger_source = "judge" if judge_result.verified_trigger else "agent"
                print(f"  → Gate A: executing {trigger_source} trigger ...")
                asan_hit, trig_stdout, trig_stderr = verifier_mod.verify_trigger(
                    trigger_script=trigger_to_run,
                    container_name=target.container_name,
                )

                _append_audit_log({
                    "run_id": run_id,
                    "event": "gate_a",
                    "asan_triggered": asan_hit,
                    "trigger_source": trigger_source,
                    "trigger_stdout": trig_stdout[:5_000],
                    "trigger_stderr": trig_stderr[:5_000],
                }, target=target)

                if asan_hit:
                    print(f"\n{'='*60}")
                    print("  *** CONFIRMED DEFECT ***")
                    print(f"  File   : {filepath.name}")
                    print(f"  run_id : {run_id}")
                    print(f"  Summary:\n{result.summary[:500]}")
                    print(f"{'='*60}\n")
                    _append_audit_log({
                        "run_id": run_id,
                        "event": "confirmed",
                        "status": "confirmed",
                        "target_file": filepath.name,
                        "summary": result.summary,
                        "findings_count": len(result.findings),
                        "diagnostic_trigger": result.diagnostic_trigger,
                    }, target=target)
                    confirmed = True
                else:
                    print("  [Gate A: no ASan signal — trigger_failed]")
                break

            elif judge_result.verdict == "RETRY":
                if retry_count >= config.MAX_RETRIES_PER_FILE:
                    print(
                        f"  [Judge: RETRY — max retries ({config.MAX_RETRIES_PER_FILE}) exhausted "
                        f"→ marking intractable]"
                    )
                    _append_audit_log({
                        "run_id": run_id,
                        "event": "intractable",
                        "status": "intractable",
                        "target_file": filepath.name,
                        "reason": "max retries exhausted after judge RETRY",
                        "judge_reasoning": judge_result.reasoning[:1000],
                    }, target=target)
                    break
                retry_count += 1
                retry_handoff = judge_result.retry_handoff
                print(f"  [Judge: RETRY #{retry_count}] {judge_result.reasoning[:200]}")
                print(f"  Handoff: {retry_handoff[:120]}...")

            elif judge_result.verdict == "INTRACTABLE":
                print(f"  [Judge: INTRACTABLE] {judge_result.reasoning[:200]}")
                _append_audit_log({
                    "run_id": run_id,
                    "event": "intractable",
                    "status": "intractable",
                    "target_file": filepath.name,
                    "reason": judge_result.reasoning[:1000],
                }, target=target)
                break

            else:  # ERROR — judge crashed or returned unparseable output
                if judge_result.reasoning == "usage_limit":
                    print(
                        f"  [Judge: HALTING — usage limit reached. "
                        f"Wait for API usage to reset, then rerun.]"
                    )
                    halt = True
                else:
                    print(f"  [Judge: ERROR — {judge_result.reasoning[:200]}]")
                    _append_audit_log({
                        "run_id": run_id,
                        "event": "judge_error",
                        "status": "judge_error",
                        "target_file": filepath.name,
                        "reason": judge_result.reasoning[:1000],
                        "judge_transcript_path": judge_result.transcript_path,
                    }, target=target)
                break

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n[orchestrator] Pipeline complete.")
    print(f"  Runs dispatched : {runs_dispatched}")
    print(f"  Total spend     : ${tracker.spent():.3f}")
    print(f"  Budget remaining: ${tracker.remaining():.3f}")
    print(f"  Confirmed       : {confirmed}")
    if halt and not confirmed:
        print(f"  Halted early    : True (usage limit or budget exhausted)")
    print(f"  Audit log       : {config.audit_log_path(target.name)}")


# ── CLI ─────────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="MiniMythos MVP — automated vulnerability discovery harness"
    )
    parser.add_argument(
        "--target", default=None,
        help="Target name (subdirectory under targets/). "
             "Falls back to MINIMYTHOS_TARGET env var or auto-detected if only one target exists."
    )
    parser.add_argument(
        "--source-dir", default=None,
        help="Override the source directory path on the host "
             "(default: derived from target.container_workdir)"
    )
    parser.add_argument(
        "--model", default=config.AUDIT_MODEL,
        help=f"Claude model for audit runs (default: {config.AUDIT_MODEL})"
    )
    parser.add_argument(
        "--budget", type=float, default=config.HARD_BUDGET_USD,
        help=f"Hard spend cap in USD (default: {config.HARD_BUDGET_USD})"
    )
    parser.add_argument(
        "--max-runs", type=int, default=None,
        help="Maximum number of audit runs to dispatch (default: unlimited)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Score files and print queue, but skip actual claude invocations"
    )
    parser.add_argument(
        "--skip-docker", action="store_true",
        help="Skip Gate A trigger execution only. The audit agent and judge still run "
             "inside Docker via 'docker exec'. Use this when you want agent output "
             "without executing potentially dangerous trigger scripts."
    )
    _default_claude_home = str(Path.home())
    parser.add_argument(
        "--claude-home", default=_default_claude_home,
        help="Path to a HOME directory with claude auth session "
             f"(default: {_default_claude_home})"
    )
    args = parser.parse_args()

    target = load_target(args.target)
    source_dir = _ensure_source_dir(target, args.source_dir)

    run_pipeline(
        target=target,
        source_dir=source_dir,
        model=args.model,
        hard_budget=args.budget,
        max_runs=args.max_runs,
        dry_run=args.dry_run,
        skip_docker=args.skip_docker,
        claude_home=args.claude_home,
    )


if __name__ == "__main__":
    main()
