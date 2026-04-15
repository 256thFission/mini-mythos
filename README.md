<div align="center">

# Mini-Mythos

<img width="600" alt="minimythos" src="https://github.com/user-attachments/assets/c57d4342-9a94-490a-8c15-83cf64bade3e" />
<hr>

<p>
<strong>A (shoddy) OSS recreation of <a href="https://red.anthropic.com/2026/mythos-preview/">Anthropic's Mythos Preview</a> Cybersecurity harness* that locates and verifes memory-safety vulnerabilities in C/C++ codebases.</strong>
</p>

<blockquote>
<sub>*AGI not included, results may vary, side effects may inlcude the end of all software, ludicrous api bills and/or anthropic account bans</sub> <br/>
<sub>(probably not but I wouldn't say never ;-;)</sub>
</blockquote>
<hr>
</div>

**Anthropic's Design is Stupidly Simple**
1. Rank every file 1-5 
2. Spin up a Docker container with an ASan-instrumented build
3. Prompt Claude Code with that file to 'find an exploit bro' and report back a defect with a reproduction script.
4. Have a Judge critic the finding for BS
5. Repeat for EVERY FILE

**that's it.**

## Okay, Why remake that.? Does it work?
Obviously, *I do not have access to Claude Mythos*. This project is an experiment in 'baking a cake without flour'. 

The hypothesis is that, with a reasonable harness, you don't need it. 
It doesn't take a genius to realize 'rate every file 1-5' is likely NOT best way to automate zero-day-discovery, and specialized tools + scaffolding might hold the key to better performance. Besides, Long term, big compute + historic CVEs + OSS git checkpoints is a perfect RL sandbox for tuninng agentic cyber-sec tools.

As for it working, early results are positive.
View [Current Progress](#current-progress) to read current progress & yapping. 


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


## Current Progress

So far, I've worked with [miniupnpd](https://github.com/miniupnp/miniupnp) as a test repo, and managed to recreate historic CVEs on older checkouts.
For novel work, the $20 opus plan has unearthed a global buffer overflow in `miniupnpd.c` remotely-triggered if running with a non-default config option. 

**Notes:*
-  Anthropic Models are surprisingly willing to just FIND vulnerabilities and write triggers in this setting. Anccetodally, very low refusal rate when prompting them to find and write trigger scripts inside this automated harness.
- Wrapping the Claude Code CLI directly is a massive shortcut. It might be too heavy and warrent changes later, but it's a SOTA agent scaffolding for a reason and mirrors what Anthropic reported in their tests.

Experimenting with better harnesses to test current model capabilities seems promising, with Opus [already finding live Firefox vulnerabilities] (https://www.anthropic.com/news/mozilla-firefox-security)

**Planned improvements/ experiments*
- Semantic taint analysis before the main agent to focus the search space
- Joern tools for call-graph analysis and reachability checks
- AST-Aware context trimming 
- Patch churn targeting
- Adding wrappers for Codex and OpenCode to benchmark performance against Claude

Obviously, PRs and issues welcome. See [Contributing](#contributing).
