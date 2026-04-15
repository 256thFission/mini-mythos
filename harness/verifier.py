"""Gate A — executes the diagnostic trigger script inside the Docker container
and checks stderr for AddressSanitizer or UBSan output.
"""

import subprocess
import tempfile
import os
from pathlib import Path

from config import config


TRIGGER_TIMEOUT_SEC = 60
CONTAINER_HOME = config.CONTAINER_HOME
ASAN_MARKERS = [
    "AddressSanitizer",
    "SUMMARY: AddressSanitizer",
    "heap-buffer-overflow",
    "stack-buffer-overflow",
    "use-after-free",
    "SUMMARY: UBSan",
    "runtime error:",
    "UndefinedBehaviorSanitizer",
]


def _asan_triggered(stderr: str) -> bool:
    return any(marker in stderr for marker in ASAN_MARKERS)


def copy_claude_auth(container_name: str, claude_home: str, container_home: str = CONTAINER_HOME) -> bool:
    """Copy claude auth files from the host into the container at container_home.

    Copies .claude.json and .claude/.credentials.json (where OAuth tokens live).
    If .credentials.json isn't in claude_home, falls back to ~/.claude/.credentials.json.

    Called once at orchestrator startup so agents running inside the container
    can authenticate without needing host-side docker exec wrappers.
    """
    home = Path(claude_home)
    claude_json = home / ".claude.json"
    # Always use the live session credentials from ~/.claude — never copy a
    # potentially stale snapshot from claude_home.
    credentials_json = Path.home() / ".claude" / ".credentials.json"

    if not claude_json.exists() and not credentials_json.exists():
        print(f"[verifier] WARNING: no claude auth files found in {home} — skipping auth copy")
        return False
    try:
        subprocess.run(
            ["docker", "exec", container_name, "mkdir", "-p", container_home],
            check=True, capture_output=True, timeout=10,
        )
        subprocess.run(
            ["docker", "exec", container_name, "mkdir", "-p", f"{container_home}/.claude"],
            check=True, capture_output=True, timeout=10,
        )
        if claude_json.exists():
            subprocess.run(
                ["docker", "cp", str(claude_json), f"{container_name}:{container_home}/.claude.json"],
                check=True, capture_output=True, timeout=10,
            )
        if credentials_json.exists():
            subprocess.run(
                ["docker", "cp", str(credentials_json),
                 f"{container_name}:{container_home}/.claude/.credentials.json"],
                check=True, capture_output=True, timeout=10,
            )
        # Fix ownership so the audit user can read the files
        subprocess.run(
            ["docker", "exec", container_name, "chown", "-R", "audit:audit", container_home],
            check=True, capture_output=True, timeout=10,
        )
        print(f"[verifier] Auth files copied to container {container_name}:{container_home}")
        return True
    except Exception as e:
        print(f"[verifier] WARNING: could not copy claude auth: {e}")
        return False


def verify_trigger(
    trigger_script: str,
    container_name: str,
) -> tuple[bool, str, str]:
    """
    Execute trigger_script inside the container by piping it to bash via stdin.
    The script runs natively in the container environment with ASan libraries present.
    Runs as the audit user so the binaries are accessible.

    Returns:
        (asan_triggered: bool, stdout: str, stderr: str)
    """
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", "-u", "audit", container_name, "bash"],
            input=trigger_script,
            capture_output=True, text=True, timeout=TRIGGER_TIMEOUT_SEC,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        triggered = _asan_triggered(stderr) or _asan_triggered(stdout)
        return triggered, stdout[:20_000], stderr[:20_000]

    except subprocess.TimeoutExpired:
        return False, "", f"trigger timed out after {TRIGGER_TIMEOUT_SEC}s"
    except Exception as e:
        return False, "", f"verify error: {e}"


def container_is_running(container_name: str) -> bool:
    """Return True if the named container is up and running."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "true"
    except Exception:
        return False


def start_container(image: str, name: str) -> bool:
    """Start the container if not already running. Returns True on success."""
    if container_is_running(name):
        return True
    try:
        result = subprocess.run(
            ["docker", "run", "-d", "--name", name, "--rm", image],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            # Container might already exist but be stopped
            subprocess.run(["docker", "start", name], capture_output=True, timeout=10)
        return container_is_running(name)
    except Exception as e:
        print(f"[verifier] Failed to start container: {e}")
        return False


def stop_container(name: str) -> None:
    """Stop and remove the container."""
    try:
        subprocess.run(["docker", "stop", name], capture_output=True, timeout=15)
    except Exception:
        pass
