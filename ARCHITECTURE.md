# MiniMythos Harness Architecture
Read this before modifying `tools/`, `harness/submit_tools.py`, or target paths.

## Quick Summary

- **Container runs the agent** — Claude Code executes inside Docker with ASan/UBSan-instrumented binaries.
- **MCP server validates submissions** — The container's MCP server (`tools/submit_mcp_server.py`) performs real-time schema + semantic validation. Invalid submissions return tool errors; Claude retries in-session.
- **Host accepts final submissions** — The host extracts the last accepted (non-error) submit from the transcript and re-validates defensively.
- **Per-target isolation** — All runtime artifacts (`audit.jsonl`, transcripts, symbols, scores) live under `runs/targets/<name>/`.
- **Schema lives in `tools/`** — Both container and host import `tools/submit_schemas.py` and `tools/submit_validators.py`. Never duplicate.

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
│   ├── submit_schemas.py      # SINGLE SOURCE OF TRUTH: tool specs + JSON Schemas
│   └── submit_validators.py   # SHARED validation logic (schema + semantic checks)
│
├── docker/
│   ├── Dockerfile         # Build for miniupnpd target (example concrete target)
│   └── Dockerfile.example # Template for new targets
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
4. **Acceptance** — Host extracts the last non-error `submit_audit_report` tool call from the transcript and re-validates defensively (single attempt + optional forced-finalization turn).
5. **Judging** — `validator.judge()` runs a second Claude session to verify "candidate" findings.
6. **Budget tracking** — `budget.BudgetTracker` enforces hard USD caps across the session.

### Container Process (Claude Code + MCP Server)

Runs inside Docker. The container image contains:
- The target source code (e.g., miniupnpd) compiled with ASan/UBSan.
- `tools/submit_mcp_server.py` — MCP stdio server exposing `submit_audit_report` and `submit_judge_verdict`.
- `mcp` and `jsonschema` Python packages (installed via pip in the Dockerfile).

Claude Code is invoked with `--mcp-config <json>` which tells it to spawn the submit server. Tool calls appear in the stream-json transcript as:
```json
{"type": "tool_use", "name": "mcp__submit__submit_audit_report", "input": {...}}
```

The MCP server **performs real validation** using `tools/submit_validators.py`. Invalid submissions return a tool result with `is_error=True` and a structured feedback payload. Claude sees this error and retries in-session without host involvement. Only valid submissions receive `is_error=False` and are considered "accepted" by the harness.

---

## Single Source of Truth: `tools/submit_schemas.py` + `tools/submit_validators.py`

**Critical invariant:** Both the container and the host must agree on tool names, descriptions, JSON Schemas, and validation logic.

**Pattern:**
- **`tools/submit_schemas.py`** defines `TOOL_SPECS: dict[str, dict[str, Any]]` with `description` and `schema` for each tool.
- **`tools/submit_validators.py`** defines `validate_audit_report()`, `validate_judge_verdict()`, and `validate_by_tool()` — shared schema + semantic validation.
- **Container:** `tools/submit_mcp_server.py` imports both modules and calls validators on every `tools/call`. Invalid calls raise, producing `is_error=True` tool results.
- **Host:** `harness/submit_tools.py` re-exports from `tools/` for defensive re-checking and fallback generation.

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

### 1. Shared validation in `tools/`
`tools/submit_validators.py` contains the validation logic used by **both** container and host. It depends on `jsonschema` (optional — validation skips if unavailable). The Dockerfile installs `mcp` and `jsonschema` so the MCP server can validate tool calls in real-time.

### 2. MCP server owns first-line validation
`tools/submit_mcp_server.py` performs schema + semantic validation on every tool call. Invalid payloads return `is_error=True` with structured feedback; Claude retries in-session. The host only sees successfully validated submissions (defensive re-check + fallback generation).

### 3. No retry loops in harness
The host no longer drives per-field retry loops (`SUBMIT_MAX_RETRIES` removed). Instead, the harness issues one main session, extracts the last accepted submit, and optionally issues a single forced-finalization turn if nothing valid arrived.

### 4. No duplicate MCP servers
Only `tools/submit_mcp_server.py` is the canonical server. Previously we had `tools/mcp_server.py` (orphan, broken import) and `tools/mcp_tool_specs.py` (orphan content file) — both deleted.

### 5. Target owns its Dockerfile
Each target directory (`targets/<name>/`) should contain:
- `target.toml` — metadata and build spec (source of truth)
- `Dockerfile` — generated from `docker/Dockerfile.tmpl` by `harness/setup_cli.py`.
  A hand-written Dockerfile at the same path overrides the template (escape hatch).

### 6. Protocol version currency
MCP `protocolVersion` fallback in `submit_mcp_server.py` should track the current spec (2025-06-18 as of this writing). The server echoes the client's version when present; the fallback is only for backward compatibility.

---

## Data Flow Example: Candidate Finding

1. **Orchestrator** calls `runner.run_audit(file, target)` with `target: TargetConfig`.
2. **Runner** spawns Claude inside container with `--mcp-config` pointing to `submit_mcp_server.py`.
3. **Agent** investigates, calls `submit_audit_report(status="candidate", ...)`.
4. **MCP server** validates the payload via `submit_validators.validate_audit_report()`:
   - If invalid: returns `is_error=True` with feedback; Claude retries in-session.
   - If valid: returns `is_error=False` with acknowledgment.
5. **Host** parses the stream-json transcript, extracts the last non-error `submit_audit_report` tool call, and re-validates defensively.
6. If accepted: **Runner** logs "audit_run" to `runs/targets/<name>/audit.jsonl` with status "candidate" and `validation_errors: []`.
7. If no valid submit arrived: **Runner** issues forced-finalization turn and re-checks; on failure emits fallback with `validation_errors` populated.
8. **Orchestrator** sees candidate, calls `validator.judge()` for independent verification.
9. **Validator** spawns second Claude session; agent calls `submit_judge_verdict(verdict="CONFIRMED", ...)` with MCP-side validation.
10. Host accepts, logs "gate_b" + final "confirmed" to audit log.
11. **Budget tracker** updates cumulative spend; if exceeded, pipeline exits.

---

## Adding a New Target

1. Write `targets/myproject/target.toml` (see `docs/ADD_NEW_TARGET.md`).
2. `python3 harness/setup_cli.py setup myproject` — renders Dockerfile, builds, runs, extracts symbols.
3. `python3 -u harness/orchestrator.py --target myproject`.

Legacy shape of `target.toml`:
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
| `ModuleNotFoundError: mcp` | Old container image without `mcp`/`jsonschema` | Rebuild with `python3 -m harness.setup_cli setup <target>` |
| Schemas out of sync | Edited `submit_schemas.py` but didn't rebuild image | Rebuild container; host and container share `tools/` via COPY |
| Validation logic divergence | Edited `harness/submit_tools.py` instead of `tools/submit_validators.py` | Put all validation in `tools/submit_validators.py` only |
| Empty `audit.jsonl` created by `watch_run.py` | `tail()` opened with `"a+"` before pipeline started | Check existence first (already fixed) |
| `submit_attempts` always 1 | Expected — host no longer drives retries | Check transcript for `is_error=True` submit calls to see in-session retries |

---

## Files You Should Know

| File | Purpose | Can edit? |
|------|---------|-----------|
| `tools/submit_schemas.py` | Tool specs + JSON Schemas (SSOT) | Yes — but rebuild image after |
| `tools/submit_validators.py` | Shared validation logic (schema + semantic) | Yes — but rebuild image after |
| `tools/submit_mcp_server.py` | In-container MCP server (validates tool calls) | Yes — uses `mcp` SDK |
| `harness/submit_tools.py` | Host-side re-exports, MCP config builder, fallbacks | Yes |
| `harness/config.py` | TargetConfig, RunConfig, path helpers | Yes — add per-target paths here |
| `harness/orchestrator.py` | Main pipeline entry | Yes |
| `harness/runner.py` | Audit run lifecycle (extracts submits, forced finalization) | Yes |
| `harness/validator.py` | Judge phase | Yes |

---

## Version History

- **2025-06-18** — MCP `protocolVersion` bumped to current spec.
- **2025-06-18** — Real MCP validation: `tools/submit_mcp_server.py` now validates tool calls using `tools/submit_validators.py`. Invalid submissions return `is_error=True`; Claude retries in-session. Host retry loops removed; `SUBMIT_MAX_RETRIES` deleted.
- **2025-04** — Per-target paths introduced; legacy flat `runs/` structure removed.
- **2025-04** — `tools/mcp_server.py` and `tools/mcp_tool_specs.py` deleted (orphans).
