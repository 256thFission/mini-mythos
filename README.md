# MiniMythos

A minimal (Shoddy) OSS recreation of [Anthropic's Claude Mythos Preview](https://red.anthropic.com/2026/mythos-preview/), an autonomous harness that finds and confirms real memory-safety vulnerabilities in C/C++ codebases.

Their design is almost stupid simple:

1. Rank every file 1-5 
2. Spin up a Docker container with an ASan-instrumented build
3. Prompt Claude Code with that file to 'find an exploit bro' and report back a defect with a reproduction script.
4. Have a Judge critic the finding for BS
5. Repeat for EVERY FILE

that's it.

Because I am poor, and this *IS* perhaps too stupidly simple we also added:

- A Dead-code 'prefilter' that compiles the binary and identifes files with zero exported symbols and dead functions with zero invocations. 
  Claude really seems to like wasting effort on commented legacy code never touched in default builds, so we skip those.  
- Resumability/ budget caps


# Why do this?

Well, it doesn't take a genius to figure out that 'rate every file 1-5' is likely NOT the best way to automate zero-day-discovery.
I want to expierment with the capabilities frontier models could reach with the right tools and guidance; Off the top of my head,

- Semantic taint analysis before the main agent to focus the search space
- Joern tools for call-graph analysis and reachability checks
- AST-Aware context trimming 
- Patch churn targeting


So far, I've worked with [miniupnpd](https://github.com/miniupnp/miniupnp) as a test repo, and managed to recreate historic CVEs on older checkouts. On the head checkout, the most impressive feat is a global buffer overflow in `miniupnpd.c`  ...triggered via a non default config option. 


 Obviously, PRs and issues welcome. See [Contributing](#contributing).

---

## Quickstart

### Prerequisites

- Docker
- Python 3.12+ (`pip install -r requirements.txt`)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) authenticated (`claude /login`)

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

The `docker build` step already generated `reachable_symbols.json` inside the container (via `nm` over every compiled `.o` file). Just copy it out:

```bash
mkdir -p runs/targets/myproject
docker cp minimythos_myproject:/opt/myproject/reachable_symbols.json \
    runs/targets/myproject/reachable_symbols.json
```

Enables dead-code filtering. If you skip it, the harness scores all files anyway.

### 4. Run

```bash
cd harness
python3 -u orchestrator.py --target myproject
```

**Flags:**

| Flag | Purpose |
|---|---|
| `--max-runs N` | Stop after N audit runs |
| `--dry-run` | Score files and print queue — skips audit runs  |
| `--skip-docker` | Skip Gate A trigger execution |
| `--budget USD` | Hard cap in USD (default: $50) |
| `--model MODEL` | Audit model (default: `claude-opus-4-6`) |

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


**Note**: re-running the same command resumes from the next unresolved file. To reset fully:

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



Working end-to-end on [miniupnpd](https://github.com/miniupnp/miniupnp) at commit `f83b5e2`. The harness has autonomously found and sanitizer-confirmed real memory-safety defects.

