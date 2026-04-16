# MiniMythos Harness Architecture

**Single source of truth for structural decisions.** Read this before modifying `tools/`, `harness/submit_tools.py`, or target paths.

## Quick Summary

- **Container runs the agent** — Claude Code executes inside Docker with ASan/UBSan-instrumented binaries.
- **Host validates submissions** — The container's MCP server accepts any tool call; the host parses the stream-json transcript and enforces schema/semantics.
- **Per-target isolation** — All runtime artifacts (`audit.jsonl`, transcripts, symbols, scores) live under `runs/targets/<name>/`.
- **Schema lives in `tools/`** — Both container and host import `tools/submit_schemas.py`. Never duplicate.

---

## Directory Layout

```
mini-mythos/
├── harness/                 # Host-side orchestration (runs on your machine)
│   ├── orchestrator.py    # Entry point: source prep → scoring → audit → judge
│   ├── runner.py          # Single audit run: Claude invocation + submit validation
│   ├── validator.py       # Judge phase: verifies candidates independently
│   ├── scorer.py          # Risk scoring: 1-5 priority for each source file
│   ├── submit_tools.py    # Host-side: validation, fallback factories, MCP config builder
│   ├── claude_client.py   # Low-level Claude CLI wrapper (--mcp-config, stream-json parsing)
│   ├── budget.py          # Global + per-run USD tracking
│   ├── config.py          # TargetConfig (from target.toml) + RunConfig (constants)
│   └── prompts/           # System prompts for audit/judge/score
│
├── tools/                 # Files copied into the container image (see Dockerfile)
│   ├── submit_mcp_server.py   # In-container MCP stdio server (spawned by Claude CLI)
│   └── submit_schemas.py      # SINGLE SOURCE OF TRUTH: tool specs + JSON Schemas
│
├── docker/
│   ├── Dockerfile         # Build for miniupnpd target (example concrete target)
│   ├── Dockerfile.example # Template for new targets
│   └── extract_symbols.py # Post-build symbol extraction for dead-code filtering
│
├── targets/
│   └── miniupnpd/
│       └── target.toml    # Target metadata: container_name, image, repo URL, etc.
│
├── runs/                  # Runtime artifacts (gitignored)
│   └── targets/
│       └── <name>/
│           ├── audit.jsonl           # Central event log (scores, runs, gates)
│           ├── scores.json             # Risk-score cache
│           ├── reachable_symbols.json  # ASan-reachable symbols per file
│           ├── transcripts/            # Per-audit-run transcripts
│           └── judge_transcripts/      # Per-judge-run transcripts
│
└── sources/               # Cloned target repos (gitignored)
    └── <name>/
```

---

## The Two-Process Model

### Host Process (`harness/orchestrator.py`)

Runs on the developer machine. Responsible for:
1. **Target loading** — `config.load_target()` reads `targets/<name>/target.toml` into `TargetConfig`.
2. **Scoring** — `scorer.score_directory()` prioritizes files (1-5) using Haiku.
3. **Audit loop** — `runner.run_audit()` spawns Claude inside the container for each high-scoring file.
4. **Validation** — `submit_tools.validate_audit_report()` parses the stream-json `tool_use` event; invalid payloads trigger retry feedback.
5. **Judging** — `validator.judge()` runs a second Claude session to verify "candidate" findings.
6. **Budget tracking** — `budget.BudgetTracker` enforces hard USD caps across the session.

### Container Process (Claude Code + MCP Server)

Runs inside Docker. The container image contains:
- The target source code (e.g., miniupnpd) compiled with ASan/UBSan.
- `tools/submit_mcp_server.py` — MCP stdio server exposing `submit_audit_report` and `submit_judge_verdict`.

Claude Code is invoked with `--mcp-config <json>` which tells it to spawn the submit server. Tool calls appear in the stream-json transcript as:
```json
{"type": "tool_use", "name": "mcp__submit__submit_audit_report", "input": {...}}
```

The MCP server **does not validate** — it returns a success envelope immediately. The host parses the transcript, extracts the `input`, and runs full schema + semantic validation.

---

## Single Source of Truth: `tools/submit_schemas.py`

**Critical invariant:** Both the container and the host must agree on tool names, descriptions, and JSON Schemas.

**Pattern:**
- **`tools/submit_schemas.py`** defines `TOOL_SPECS: dict[str, dict[str, Any]]` with `description` and `schema` for each tool.
- **Container:** `tools/submit_mcp_server.py` imports `TOOL_SPECS` to advertise tools in `tools/list`.
- **Host:** `harness/submit_tools.py` imports `TOOL_SPECS` and uses the schemas for validation via `jsonschema`.

**Never duplicate schemas.** If you need to change a field:
1. Edit `tools/submit_schemas.py`.
2. Rebuild the container image (so the new `submit_schemas.py` is inside).
3. The host sees the same code via filesystem import (tools/ is on PYTHONPATH via sys.path insert).

### Tool Naming

In the stream-json transcript, tool names are namespaced as:
```
mcp__<serverName>__<toolName>
```

The harness uses `submit_tools.submit_tool_name("submit_audit_report")` → `"mcp__submit__submit_audit_report"` to detect submissions.

---

## Per-Target Path Structure

**Pre-2024:** All artifacts went to flat `runs/` directories (legacy; removed).

**Current:** Everything is namespaced by target:

| Artifact | Path (via `config.py`) |
|----------|------------------------|
| Audit log | `runs/targets/<name>/audit.jsonl` |
| Score cache | `runs/targets/<name>/scores.json` |
| Reachable symbols | `runs/targets/<name>/reachable_symbols.json` |
| Audit transcripts | `runs/targets/<name>/transcripts/` |
| Judge transcripts | `runs/targets/<name>/judge_transcripts/` |
| Source cache | `sources/<name>/` |

**Code locations:**
- `config.target_runs_dir(name)` → `Path("runs/targets/<name>")`
- `config.audit_log_path(name)` → `Path("runs/targets/<name>/audit.jsonl")`
- `runner._save_transcript()` uses `config.target_runs_dir(target_name) / "transcripts"`
- `validator.judge()` uses `config.target_runs_dir(target.name) / "judge_transcripts"`

**No legacy fallbacks remain.** The `target` parameter is required; if it's missing, the code fails fast rather than falling back to global paths.

---

## Key Invariants

### 1. Container-side code is stdlib-only
`tools/submit_mcp_server.py` and `tools/submit_schemas.py` cannot import third-party packages (no `jsonschema`, no `pydantic`). The Dockerfile does not install them. Keep these files pure stdlib.

### 2. Host-side code owns validation
`harness/submit_tools.py` uses `jsonschema` (if available) plus hand-written semantic checks (e.g., `status=candidate` requires `diagnostic_trigger`). Invalid submissions result in retry feedback injected back into the agent context.

### 3. No duplicate MCP servers
Only `tools/submit_mcp_server.py` is the canonical server. Previously we had `tools/mcp_server.py` (orphan, broken import) and `tools/mcp_tool_specs.py` (orphan content file) — both deleted.

### 4. Target owns its Dockerfile
Each target directory (`targets/<name>/`) should contain:
- `target.toml` — metadata
- `Dockerfile` — build instructions (copied from `docker/Dockerfile.example` as template)

The `miniupnpd` target currently violates this (uses `docker/Dockerfile`). Fix: move it to `targets/miniupnpd/Dockerfile` so the orchestrator error message (`-f targets/<name>/Dockerfile`) works correctly.

### 5. Protocol version currency
MCP `protocolVersion` fallback in `submit_mcp_server.py` should track the current spec (2025-06-18 as of this writing). The server echoes the client's version when present; the fallback is only for backward compatibility.

---

## Data Flow Example: Candidate Finding

1. **Orchestrator** calls `runner.run_audit(file, target)` with `target: TargetConfig`.
2. **Runner** spawns Claude inside container with `--mcp-config` pointing to `submit_mcp_server.py`.
3. **Agent** investigates, calls `submit_audit_report(status="candidate", ...)`.
4. **MCP server** returns `{"content": [{"type": "text", "text": "submit_audit_report received"}]}`.
5. **Host** parses the stream-json `tool_use` event, extracts `input`, runs `submit_tools.validate_audit_report(input)`.
6. If valid: **Runner** logs "audit_run" to `runs/targets/<name>/audit.jsonl` with status "candidate".
7. **Orchestrator** sees candidate, calls `validator.judge()` for independent verification.
8. **Validator** spawns second Claude session, agent calls `submit_judge_verdict(verdict="CONFIRMED", ...)`.
9. Host validates, logs "gate_b" + final "confirmed" to audit log.
10. **Budget tracker** updates cumulative spend; if exceeded, pipeline exits.

---

## Adding a New Target

1. `mkdir targets/myproject && cd targets/myproject`
2. `cp ../../docker/Dockerfile.example Dockerfile`
3. Edit `Dockerfile`: clone your repo, build with ASan/UBSan, install Node.js + claude-code.
4. Create `target.toml`:
   ```toml
   [project]
   name = "myproject"
   description = "..."

   [docker]
   container_name = "minimythos_myproject"
   image = "minimythos_myproject:latest"
   workdir = "/opt/myproject"

   [build]
   repo_url = "https://github.com/..."
   repo_revision = "abc123"
   build_dir = "/opt/myproject/build"
   ```
5. Build: `docker build -t minimythos_myproject:latest -f targets/myproject/Dockerfile .`
6. Run: `python3 -m harness.orchestrator --target myproject`

---

## Common Pitfalls

| Pitfall | Why it happens | Fix |
|---------|---------------|-----|
| `ModuleNotFoundError: submit_spec` | Trying to run `tools/mcp_server.py` (orphan) | Use `tools/submit_mcp_server.py` only |
| Schemas out of sync | Edited `submit_schemas.py` but didn't rebuild image | Rebuild container after schema changes |
| `targets/<name>/Dockerfile not found` | miniupnpd uses `docker/Dockerfile` instead of per-target path | Move Dockerfile into target directory |
| Empty `audit.jsonl` created by `watch_run.py` | `tail()` opened with `"a+"` before pipeline started | Check existence first (already fixed) |
| Judge transcripts in wrong dir | Legacy fallback path still present | Remove `if target else` branches (already fixed) |

---

## Files You Should Know

| File | Purpose | Can edit? |
|------|---------|-----------|
| `tools/submit_schemas.py` | Tool specs + JSON Schemas (SSOT) | Yes — but rebuild image after |
| `tools/submit_mcp_server.py` | In-container MCP server | Yes — keep stdlib-only |
| `harness/submit_tools.py` | Host-side validation, MCP wiring | Yes |
| `harness/config.py` | TargetConfig, RunConfig, path helpers | Yes — add per-target paths here |
| `harness/orchestrator.py` | Main pipeline entry | Yes |
| `harness/runner.py` | Audit run lifecycle | Yes |
| `harness/validator.py` | Judge phase | Yes |

---

## Version History

- **2025-06-18** — MCP `protocolVersion` bumped to current spec.
- **2025-04** — Per-target paths introduced; legacy flat `runs/` structure removed.
- **2025-04** — `tools/mcp_server.py` and `tools/mcp_tool_specs.py` deleted (orphans).
