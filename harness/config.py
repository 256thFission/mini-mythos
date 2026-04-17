"""Centralized configuration for MiniMythos harness.

All magic numbers, timeouts, model names, and paths live here.
Project-specific values (container name, workdir, image, etc.) are loaded
from targets/<name>/target.toml via TargetConfig / load_target().
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


@dataclass(frozen=True)
class TargetConfig:
    """Per-project settings loaded from targets/<name>/target.toml."""
    name: str
    description: str
    container_name: str
    container_image: str
    container_workdir: str
    repo_url: str = ""
    repo_revision: str = ""
    build_dir: str = ""
    # Subdirectory under the cloned repo that contains the *.c/*.h files to
    # score. Defaults to build_dir when unset — they match for most projects.
    # Dropbear is the counterexample: build_dir="." but src_dir="src".
    src_dir: str = ""
    # New template-driven fields (consumed by harness.setup_cli to render a
    # Dockerfile). When a hand-written Dockerfile exists at the target path,
    # these are informational only.
    apt_packages: tuple[str, ...] = ()
    build_commands: tuple[str, ...] = ()
    # Absolute paths (inside the container) of pre-built, ASan/UBSan-instrumented
    # binaries the audit agent should run directly. Rendered into the audit
    # prompt so the agent doesn't waste turns rebuilding.
    binaries: tuple[str, ...] = ()


def load_target(name: str | None = None) -> TargetConfig:
    """Load a TargetConfig from targets/<name>/target.toml.

    Resolution order:
      1. ``name`` argument
      2. ``MINIMYTHOS_TARGET`` environment variable
      3. Auto-detect if only one target exists under targets/
    """
    base = Path(__file__).parent.parent / "targets"

    if name is None:
        name = os.environ.get("MINIMYTHOS_TARGET")

    if name is None:
        candidates = [d for d in base.iterdir() if d.is_dir() and (d / "target.toml").exists()]
        if len(candidates) == 1:
            name = candidates[0].name
        elif len(candidates) == 0:
            print("[config] ERROR: No targets found. Create targets/<name>/target.toml first.")
            sys.exit(1)
        else:
            names = sorted(d.name for d in candidates)
            print(f"[config] ERROR: Multiple targets found: {names}. Pass --target <name>.")
            sys.exit(1)

    toml_path = base / name / "target.toml"
    if not toml_path.exists():
        print(f"[config] ERROR: {toml_path} not found.")
        sys.exit(1)

    if tomllib is None:
        print("[config] ERROR: tomllib/tomli not available. Install tomli: pip install tomli")
        sys.exit(1)

    raw = tomllib.loads(toml_path.read_text())
    project = raw.get("project", {})
    build = raw.get("build", {})
    binaries_tbl = raw.get("binaries", {})
    if not isinstance(binaries_tbl, dict):
        print(f"[config] ERROR: {toml_path} [binaries] must be a table with a 'paths' array")
        sys.exit(1)
    if not binaries_tbl.get("paths"):
        print(f"[config] ERROR: {toml_path} missing [binaries].paths (list of absolute paths to pre-built instrumented binaries)")
        sys.exit(1)

    # Schema is strictly defined in [build] section - no backwards compatibility
    workdir = build.get("workdir", "")
    if not workdir:
        print(f"[config] ERROR: {toml_path} missing [build].workdir")
        sys.exit(1)

    commands = build.get("commands", [])
    if not commands:
        print(f"[config] ERROR: {toml_path} missing [build].commands")
        sys.exit(1)

    return TargetConfig(
        name=name,
        description=project.get("description", ""),
        container_name=f"minimythos_{name}",
        container_image=f"minimythos_{name}:latest",
        container_workdir=workdir,
        repo_url=build.get("repo_url", ""),
        repo_revision=build.get("repo_revision", ""),
        build_dir=build.get("build_dir", ""),
        src_dir=build.get("src_dir", build.get("build_dir", "")),
        apt_packages=tuple(build.get("apt_packages", [])),
        build_commands=tuple(commands),
        binaries=tuple(binaries_tbl.get("paths", [])),
    )


@dataclass(frozen=True)
class RunConfig:
    """Strictly typed configuration for the entire harness."""

    # Budget settings
    HARD_BUDGET_USD: float = 50.00
    PER_RUN_BUDGET_USD: float = 4.00
    JUDGE_MAX_BUDGET_USD: float = 2.00
    MIN_RUN_COST_USD: float = 1.00

    # Model settings
    AUDIT_MODEL: str = "claude-opus-4-6"
    SCORE_MODEL: str = "claude-haiku-4-5-20251001"
    JUDGE_MODEL: str = "claude-sonnet-4-6"

    # Timeouts
    RUN_TIMEOUT_SEC: int = 1800
    SCORE_TIMEOUT_SEC: int = 120
    JUDGE_TIMEOUT_SEC: int = 400

    # Limits
    MAX_RETRIES_PER_FILE: int = 2
    RUN_MAX_TURNS: int = 50
    JUDGE_MAX_TURNS: int = 25

    # Docker — shared non-project settings
    CONTAINER_HOME: str = "/audit-home"
    # Absolute path inside the container where submit_mcp_server.py is baked
    # into the image (see docker/Dockerfile — COPY tools/ /opt/minimythos/tools/).
    # Claude spawns it from here via --mcp-config.
    CONTAINER_MCP_SERVER_PATH: str = "/opt/minimythos/tools/submit_mcp_server.py"

    # Scoring settings
    MAX_FILE_BYTES: int = 80_000
    DEFAULT_SCORE: int = 3

    # Base paths
    @property
    def BASE_DIR(self) -> Path:
        return Path(__file__).parent.parent

    @property
    def RUNS_DIR(self) -> Path:
        return self.BASE_DIR / "runs"

    @property
    def ARTIFACTS_DIR(self) -> Path:
        return self.RUNS_DIR / "artifacts"

    @property
    def PROMPTS_DIR(self) -> Path:
        return Path(__file__).parent / "prompts"

    # Legacy global paths (kept for show_run.py / watch_run.py / budget.py)
    @property
    def SCORE_CACHE(self) -> Path:
        return self.RUNS_DIR / "scores.json"

    @property
    def AUDIT_LOG(self) -> Path:
        return self.RUNS_DIR / "audit.jsonl"

    # Per-target path helpers
    def target_runs_dir(self, target_name: str) -> Path:
        return self.RUNS_DIR / "targets" / target_name

    def audit_log_path(self, target_name: str) -> Path:
        return self.target_runs_dir(target_name) / "audit.jsonl"

    def score_cache_path(self, target_name: str) -> Path:
        return self.target_runs_dir(target_name) / "scores.json"

    def source_cache_dir(self, target_name: str) -> Path:
        return self.BASE_DIR / "sources" / target_name


# Module-level singleton instance - import this everywhere
config = RunConfig()
