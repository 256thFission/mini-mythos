<div align="center">

# Mini-Mythos

<img width="600" alt="minimythos" src="https://github.com/user-attachments/assets/c57d4342-9a94-490a-8c15-83cf64bade3e" />
<hr>

<p>
<strong>A (shoddy) OSS clone of <a href="https://red.anthropic.com/2026/mythos-preview/">Anthropic's Mythos Preview</a> cybersecurity harness* to locate and verify memory-safety vulnerabilities in C/C++ codebases.</strong>
</p>

<blockquote>
<sub>*AGI not included, results may vary, side effects may include the end of all software, ludicrous API bills and/or Anthropic account bans</sub> <br/>
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

## Okay, Why remake that? Does it work?
Obviously, *I do not have access to Claude Mythos*. This project is an experiment in 'baking a cake without flour'. 

The hypothesis is that, with a reasonable harness, you don't need it. 
It doesn't take a genius to realize 'rate every file 1-5' is likely NOT best way to automate zero-day-discovery, and specialized tools + scaffolding might hold the key to better performance. Besides, Long term, big compute + historic CVEs + OSS git checkpoints is a perfect RL sandbox for tuninng agentic cyber-sec tools.

As for it working, early results are positive.
View [Current Progress](#current-progress) to read current progress & yapping. 


## Quickstart - 1 step setup
### Prerequisites

- Docker
- Python 3.11+ (`pip install -r requirements.txt`)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 

### 1. Configure your target

Create `targets/<name>/target.toml` 

```toml
[project]
name = "myproject"
description = "a short description"

[build]
repo_url = "https://github.com/example/myproject.git"
repo_revision = "abc123"              # pin a commit SHA
workdir = "/opt/myproject"            # inside-container source path
build_dir = "."                       # subdir where commands run
apt_packages = ["libssl-dev"]         # extras on top of the base image

commands = [
    "./configure",
    "make CC=clang CFLAGS='-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined' LDFLAGS='-fsanitize=address,undefined'",
]

[symbols]
object_glob = "*.o"                   # "build/**/*.o" for CMake
source_exts = [".c"]                  # add ".cc"/".cpp" for C++
```

### 2. Set up the container

```bash
python3 harness/setup_cli.py setup myproject
```

That renders `targets/myproject/Dockerfile` from `docker/Dockerfile.tmpl`,
builds the image, starts the container, and copies `reachable_symbols.json`
to `runs/targets/myproject/`.

**WARNING:** If your project needs exotic build steps (custom base image,
multi-stage build, pre-build patches), Your're on your own. Write to `targets/<name>/Dockerfile`. The setup CLI detects it, Use `--force-render` to overwrite.

### 3. Run

```bash
python3 -u harness/orchestrator.py
```

| Flag | Purpose |
|---|---|
| `--target NAME` | Target to audit (auto-detected if only one exists) |
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

## Current Progress

So far, I've worked with [miniupnpd](https://github.com/miniupnp/miniupnp) as a test repo, and managed to recreate historic CVEs on older checkouts.
For novel work, the $20 opus plan has unearthed a global buffer overflow in `miniupnpd.c` remotely-triggered if running with a non-default config option. 

**Notes:**
-  Anthropic Models are surprisingly willing to just FIND vulnerabilities and write triggers in this setting. Anccetodally, very low refusal rate when prompting them to find and write trigger scripts inside this automated harness.
- Wrapping the Claude Code CLI directly is a massive shortcut. It might be too heavy and warrent changes later, but it's a SOTA agent scaffolding for a reason and mirrors what Anthropic reported in their tests.

Experimenting with better harnesses to test current model capabilities seems promising, with Opus [already finding live Firefox vulnerabilities] (https://www.anthropic.com/news/mozilla-firefox-security)

**Planned improvements/ experiments**
- Semantic taint analysis before the main agent to focus the search space
- Joern tools for call-graph analysis and reachability checks
- AST-Aware context trimming 
- Patch churn targeting
- Adding wrappers for Codex and OpenCode to benchmark performance against Claude

Obviously, PRs and issues welcome.
