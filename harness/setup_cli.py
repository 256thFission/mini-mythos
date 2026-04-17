"""One-shot setup CLI: config → Dockerfile → build → run → extract symbols.

Typical use:

    python3 -m harness.setup_cli setup dropbearssh

Reads ``targets/<name>/target.toml``, renders a Dockerfile from
``docker/Dockerfile.tmpl`` (unless a hand-written Dockerfile already exists in
the target directory — that always wins as an escape hatch), then:

    1. ``docker build -t <image> -f targets/<name>/Dockerfile .``
    2. ``docker rm -f <container>`` (if present) + ``docker run -d --name ...``
    3. ``docker cp <container>:<build_workdir>/reachable_symbols.json
                  runs/targets/<name>/reachable_symbols.json``

After this, ``orchestrator.py --target <name>`` is the only remaining step.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

_HARNESS_DIR = Path(__file__).resolve().parent
if str(_HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(_HARNESS_DIR))

from config import TargetConfig, config, load_target  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
TMPL_PATH = REPO_ROOT / "docker" / "Dockerfile.tmpl"


# ── Template rendering ──────────────────────────────────────────────────────

def _build_workdir(target: TargetConfig) -> str:
    """Absolute path inside the container where build commands run."""
    workdir = Path(target.container_workdir)
    if target.build_dir and target.build_dir not in (".", ""):
        return str(workdir / target.build_dir)
    return str(workdir)


def render_dockerfile(target: TargetConfig) -> str:
    if not target.repo_url or not target.repo_revision or not target.container_workdir:
        print(
            "[setup] ERROR: target.toml is missing repo_url / repo_revision / "
            "workdir. Cannot render Dockerfile.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not target.build_commands:
        print(
            "[setup] ERROR: target.toml has no [build].commands. "
            "Add at least one command (e.g. './configure' and 'make ...').",
            file=sys.stderr,
        )
        sys.exit(2)

    # Bug 4 & 7 fix: Escape newlines in commands to prevent injection; handle empty apt_packages
    def escape_dockerfile_cmd(cmd: str) -> str:
        # Prevent command injection via newlines - only allow single-line RUN instructions
        return cmd.replace('\n', ' ').replace('\r', ' ')

    run_lines = "\n".join(f"RUN {escape_dockerfile_cmd(cmd)}" for cmd in target.build_commands)

    # Bug 4 fix: Handle empty apt_packages - omit the line entirely if empty
    apt_packages_line = " ".join(target.apt_packages)
    if apt_packages_line:
        apt_packages_line += " \\"

    ctx = {
        "name": target.name,
        "apt_packages": apt_packages_line,
        "repo_url": target.repo_url,
        "repo_revision": target.repo_revision,
        "workdir": target.container_workdir,
        "build_workdir": _build_workdir(target),
        "build_run_lines": run_lines,
        "object_glob": target.symbols_object_glob,
        "source_exts": ",".join(target.symbols_source_exts),
    }
    tmpl = TMPL_PATH.read_text()
    # Use a placeholder that won't appear in values to prevent double-replacement
    placeholder_prefix = uuid.uuid4().hex[:16]
    placeholders = {}

    # First pass: replace {{key}} with unique placeholders
    for key, val in ctx.items():
        placeholder = f"__{placeholder_prefix}_{key}__"
        placeholders[placeholder] = val
        tmpl = tmpl.replace("{{" + key + "}}", placeholder)

    # Second pass: replace placeholders with actual values
    for placeholder, val in placeholders.items():
        tmpl = tmpl.replace(placeholder, val)

    return tmpl


# ── Docker helpers ───────────────────────────────────────────────────────────

def _run(cmd: list[str], **kw) -> None:
    print(f"[setup] $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True, **kw)


def _docker_ok() -> bool:
    return shutil.which("docker") is not None and (
        subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_setup(args: argparse.Namespace) -> None:
    target = load_target(args.target)
    target_dir = REPO_ROOT / "targets" / target.name
    dockerfile = target_dir / "Dockerfile"

    # Escape hatch: respect a hand-written Dockerfile.
    if dockerfile.exists() and not args.force_render:
        first = dockerfile.read_text().splitlines()[0] if dockerfile.stat().st_size else ""
        is_generated = "auto-generated" in first
        if is_generated:
            print(f"[setup] Re-rendering {dockerfile} from template ...")
            dockerfile.write_text(render_dockerfile(target))
        else:
            print(
                f"[setup] Using existing hand-written {dockerfile} "
                f"(pass --force-render to overwrite)."
            )
    else:
        target_dir.mkdir(parents=True, exist_ok=True)
        print(f"[setup] Rendering {dockerfile} from template ...")
        dockerfile.write_text(render_dockerfile(target))

    if args.render_only:
        print("[setup] --render-only: stopping after Dockerfile generation.")
        return

    if not _docker_ok():
        print(
            "[setup] ERROR: docker daemon not reachable. Start Docker Desktop "
            "(and enable WSL integration if on Windows), then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 1. Build
    _run(
        [
            "docker", "build",
            "-t", target.container_image,
            "-f", str(dockerfile),
            ".",
        ],
        cwd=str(REPO_ROOT),
    )

    # 2. Replace container (with verification to avoid race condition)
    subprocess.run(
        ["docker", "rm", "-f", target.container_name],
        capture_output=True,
    )
    # Wait up to 10s for container to actually disappear
    for _ in range(20):
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{target.container_name}$", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        )
        if target.container_name not in result.stdout:
            break
        time.sleep(0.5)
    _run([
        "docker", "run", "-d",
        "--name", target.container_name,
        target.container_image,
    ])

    # 3. Copy the symbol map out
    dest = config.reachable_symbols_path(target.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = f"{target.container_name}:{_build_workdir(target)}/reachable_symbols.json"
    _run(["docker", "cp", src, str(dest)])

    print()
    print(f"[setup] OK — {target.name} is ready.")
    print(f"        image      : {target.container_image}")
    print(f"        container  : {target.container_name}")
    print(f"        symbols    : {dest}")
    print(f"        next step  : python3 -u harness/orchestrator.py --target {target.name}")


def cmd_render(args: argparse.Namespace) -> None:
    """Render a Dockerfile without touching docker."""
    args.render_only = True
    args.force_render = True
    cmd_setup(args)


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="Render Dockerfile, build image, run container, extract symbols.")
    s.add_argument("target", help="Target name under targets/")
    s.add_argument("--render-only", action="store_true", help="Stop after writing the Dockerfile.")
    s.add_argument("--force-render", action="store_true", help="Overwrite an existing hand-written Dockerfile.")
    s.set_defaults(func=cmd_setup)

    r = sub.add_parser("render", help="Only write targets/<name>/Dockerfile (no docker).")
    r.add_argument("target")
    r.set_defaults(func=cmd_render)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
