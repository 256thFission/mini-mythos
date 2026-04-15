# MiniMythos

A minimal (Shoddy) OSS recreation of [Anthropic's Claude Mythos Preview](https://red.anthropic.com/2026/mythos-preview/), an autonomous harness that finds and confirms real memory-safety vulnerabilities in C/C++ codebases.

Their design is almost stupid simple:

[the point is to show how simple their design is here;]
The design is deliberately simple: a five-stage loop and ~1,500 lines of Python. Claude does the security research; the harness manages budget, orchestration, and verification.

---

## How it works

**1. Filter** source files against the compiled binary's symbol table (`reachable_symbols.json`, extracted via `nm` during the Docker build). Files with zero exported symbols are skipped entirely. For surviving files, [tree-sitter](https://tree-sitter.github.io/tree-sitter/) identifies individual functions absent from the binary and injects them as a prompt annotation — so the audit agent doesn't waste turns on dead code.

**2. Score** surviving `.c`/`.h` files 1–5 for attack-surface likelihood using a cheap Haiku call per file. Scores are cached; re-runs skip already-scored files.

**3. Audit** high-scored files by running Claude Opus autonomously inside an ASan/UBSan-instrumented Docker container. The agent has direct access to the binary, gdb, and the full source tree — it forms hypotheses, runs crafted inputs, inspects memory, and writes test programs until it either triggers a defect or exhausts its budget. It reports back a defect description and a verified shell script that reproduces the issue.

**4. Judge (Gate B)** any candidate finding with an independent Claude Sonnet agent that re-investigates from scratch. It applies three rejection filters before confirming:

- **Synthetic trigger**: does the reproduction script call the vulnerable function directly with fabricated arguments that no real network path would produce? If so, it's not a real attack.
- **Sanitizer-only UB**: does the sanitizer fire, but the runtime behavior is identical on all realistic platforms anyway? (e.g. signed integer overflow that compilers handle predictably as two's-complement — the bug is a code quality issue, not a security finding.)
- **Dead-code gate**: is the vulnerable code actually compiled into the default build, or gated behind a `#ifdef` that's never set?

**5. Verify (Gate A)** confirmed findings by executing the trigger script verbatim in the container and checking stderr for ASan/UBSan output. No sanitizer signal = rejected.

All events are written to an append-only JSONL log. The pipeline is **idempotent** — restarting resumes from the next unresolved file. It halts automatically on the first confirmed defect or when the budget cap is hit.

---

## Quick start

### Prerequisites

- Docker
- Python 3.9+ (`pip install -r requirements.txt`)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude /login`)

### 1. Configure your target

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
build_dir = "subdir"        # optional: subdirectory within the repo to audit
```

### 2. Build the instrumented container

Copy `docker/Dockerfile.example` to your target directory, fill in the build commands (autotools and CMake examples are included), then:

```bash
docker build -t minimythos_myproject:latest targets/myproject/
docker run -d --name minimythos_myproject minimythos_myproject:latest
```

### 3. Copy the symbol map to the host

```bash
mkdir -p runs/targets/myproject
docker cp minimythos_myproject:/opt/myproject/reachable_symbols.json \
    runs/targets/myproject/reachable_symbols.json
```

This enables dead-code filtering and saves ~$1–2 in scoring costs. If you skip it, the harness will warn and score all files.

### 4. Run

```bash
cd harness
python3 -u orchestrator.py --target myproject
```

If `repo_url` is set in `target.toml`, the orchestrator auto-clones the source on first run. Pass `--source-dir` to use a local checkout instead.

**Flags:**

| Flag | Purpose |
|---|---|
| `--max-runs N` | Stop after N audit runs |
| `--dry-run` | Score files and print queue — no Claude calls |
| `--skip-docker` | Skip Gate A trigger execution |
| `--budget USD` | Hard cap in USD (default: $50) |
| `--model MODEL` | Audit model (default: `claude-opus-4-6`) |

---

## Monitoring

```bash
# Live color log (run in a second terminal)
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
rm runs/targets/myproject/audit.jsonl
rm runs/targets/myproject/scores.json
```

---

## Architecture

```
orchestrator.py          main loop
  preprocessor.py        dead-code filter (tree-sitter + symbol table)
  scorer.py              LLM file scoring (host-side Claude)
  runner.py              audit agent dispatch (docker exec claude)
  validator.py           Gate B: independent judge agent
  verifier.py            Gate A: trigger execution + sanitizer check
  budget.py              hard spend cap enforcement
  config.py              all settings (models, timeouts, budgets)
  prompts/
    score.txt            file scoring prompt
    audit.txt            audit agent prompt
    judge.txt            judge agent prompt
```

The audit and judge agents run **inside** the container via `docker exec` — they have direct access to the compiled binary, gdb, and the full source tree. The harness coordinates from the host.

---

## Cost

Ballpark for a ~70-file C project:

| Phase | Model | Cost |
|---|---|---|
| Scoring | Haiku | ~$0.05–0.10 / file |
| Audit run | Opus | ~$1.50–4.00 / run |
| Judge run | Sonnet | ~$0.50–2.00 / run |

A full run to first confirmed finding typically costs **$15–30**.

---

## Status

Working end-to-end on [miniupnpd](https://github.com/miniupnp/miniupnp) at commit `f83b5e2`. The harness has autonomously found and sanitizer-confirmed real memory-safety defects.

Active work: better prompt engineering, smarter file selection, parallel audit runs. See open issues.

---

## Contributing

The codebase is small and meant to be hackable. Good places to start:

- **Prompts** — `harness/prompts/audit.txt` and `judge.txt` are the highest-leverage thing to improve. Better prompts = better findings.
- **New targets** — add a `targets/<name>/target.toml` and a Dockerfile, send a PR. Feedback on the setup experience is especially useful.
- **Pipeline improvements** — smarter scoring, parallel runs, tighter slop filter logic, better cost attribution.
- **Bug reports** — if the harness crashes or produces nonsense, open an issue with your `audit.jsonl` and the command you ran.

PRs and issues welcome.
