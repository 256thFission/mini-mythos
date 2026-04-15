# MiniMythos

A thin CLI harness that runs Claude Code autonomously against a C/C++ codebase to find real, sanitizer-confirmed memory-safety vulnerabilities.

Inspired by Anthropic's Claude Mythos Preview scaffold: an isolated container, a one-paragraph audit prompt, and Claude Code doing the work.

---

## What it does

1. **Scores** source files 1–5 by attack-surface likelihood (cheap LLM call per file)
2. **Audits** high-scored files with an autonomous Claude Code agent inside a Docker container (ASan/UBSan instrumented build)
3. **Judges** any candidate finding with an independent agent (Gate B) — checks reachability, SL-1/SL-2/SL-3 slop filters
4. **Executes** the verified trigger in the container and checks for sanitizer output (Gate A)
5. **Logs** everything to an append-only JSONL file; resumes cleanly after interruption

The pipeline halts automatically when a confirmed defect is found or the budget cap is hit.

---

## Quick start

### 1. Build the container

```bash
docker build -t minimythos:0.1 docker/
docker run -d --name minimythos_run minimythos:0.1
```

### 2. Copy source files and symbol map to host

```bash
mkdir -p src
docker cp minimythos_run:/opt/miniupnp/miniupnpd/. src/

mkdir -p runs/targets/miniupnpd
docker cp minimythos_run:/opt/miniupnp/miniupnpd/reachable_symbols.json runs/targets/miniupnpd/
```

The symbol map is used to skip dead-code files before scoring (saves ~$1–2).

### 3. Install Python dependencies

```bash
pip install -r requirements.txt   # only needed on Python < 3.11
```

You also need the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude /login`).

### 4. Run

```bash
cd harness
python3 -u orchestrator.py --source-dir ../src
```

**Useful flags:**

| Flag | Purpose |
|---|---|
| `--max-runs N` | Stop after N audit runs |
| `--dry-run` | Score files, print queue, no Claude calls |
| `--skip-docker` | Skip Gate A trigger execution |
| `--budget USD` | Override hard cap (default: $50) |
| `--model MODEL` | Override audit model (default: `claude-opus-4-6`) |

---

## Monitoring

```bash
# Live color log (recommended — run in a second terminal)
python3 watch_run.py --tail

# List completed runs
python3 show_run.py

# View a specific run transcript (any prefix of run_id works)
python3 show_run.py <run_id>

# View the judge transcript for a run
python3 show_run.py --judge <run_id>
```

---

## Output

| Path | Contents |
|---|---|
| `runs/targets/<name>/audit.jsonl` | Append-only event log (scoring, verdicts, gate results) |
| `runs/targets/<name>/scores.json` | Cached file scores — delete to re-score |
| `runs/transcripts/` | Per-run agent event streams |
| `runs/judge_transcripts/` | Per-run judge event streams |
| `runs/artifacts/<run_id>/` | Source copy + `metadata.json` for each run |

The pipeline is **idempotent**: re-running the same command resumes from the next unresolved file. To reset fully:

```bash
rm runs/targets/miniupnpd/audit.jsonl
rm runs/targets/miniupnpd/scores.json
```

---

## Adding a new target

Create `targets/<name>/target.toml`:

```toml
[project]
name = "myproject"
description = "a short description"

[docker]
container_name = "minimythos_myproject"
image = "minimythos_myproject:latest"
workdir = "/opt/myproject"

[build]
repo_url = "https://github.com/example/myproject.git"
repo_revision = "abc123"
```

Then write a `docker/Dockerfile` that clones the repo, builds with ASan, and runs the `reachable_symbols.json` extraction step (see the existing Dockerfile for the pattern). Pass `--target myproject` to the orchestrator.

---

## Architecture

```
orchestrator.py          main loop
  scorer.py              LLM file scoring (host-side claude)
  runner.py              audit agent dispatch (docker exec claude)
  validator.py           Gate B: independent judge agent
  verifier.py            Gate A: trigger execution + sanitizer check
  budget.py              hard spend cap enforcement
  config.py              all settings (models, timeouts, budgets)
  prompts/
    score.txt            file scoring prompt
    audit.txt            main audit prompt
    judge.txt            judge agent prompt
```

The audit agent runs **inside** the container via `docker exec` — it has direct access to the compiled binary, gdb, and the full source tree. The harness coordinates from outside.

---

## Cost

For `miniupnpd` (~72 source files):

- Scoring pass: ~$5 total (Haiku, one call per file)
- Audit run: ~$1.50–$4.00 (Opus, up to 50 turns)
- Judge run: ~$0.50–$2.00 (Sonnet)

A full run to first confirmed finding typically costs $15–$30.

---

## Status

Working end-to-end on `miniupnpd` at git hash `f83b5e2`. The harness has found and sanitizer-confirmed real CVEs autonomously.

Generalization to arbitrary C/C++ targets (pluggable Dockerfile, template prompts) is the next planned phase.
