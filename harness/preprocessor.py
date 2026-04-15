"""Dead-function annotation for audit prompts.

Uses tree-sitter to extract all C function definitions from a source file,
then cross-references against the per-target reachable_symbols.json (built
from nm output during the Docker image build) to identify functions that are
absent from the compiled binary.  The result is a plain-text block injected
into the audit prompt so the agent avoids wasting turns on dead code paths.

Requires: tree-sitter>=0.23.0  tree-sitter-c>=0.23.0
If those packages are absent the module degrades silently (empty annotation).
This module is intentionally generic — it has no knowledge of any specific
target project.
"""
from __future__ import annotations

import json
from pathlib import Path

try:
    import tree_sitter_c
    from tree_sitter import Language, Parser as _TSParser

    _C_LANGUAGE = Language(tree_sitter_c.language())
    _parser = _TSParser(_C_LANGUAGE)
    TREE_SITTER_AVAILABLE = True
except ImportError:  # pragma: no cover
    TREE_SITTER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Internal tree-sitter helpers
# ---------------------------------------------------------------------------

def _fn_name_from_declarator(node) -> str | None:
    """Recursively walk a C declarator chain and return the function identifier.

    Handles direct, pointer-return, and parenthesized declarators, e.g.:
      int   foo(...)               → function_declarator → identifier
      int  *bar(...)               → pointer_declarator  → function_declarator → identifier
      void (*baz)(...)             → not a definition, skipped by caller
    """
    if node is None:
        return None
    if node.type == "identifier":
        return node.text.decode("utf-8")
    if node.type in (
        "function_declarator",
        "pointer_declarator",
        "parenthesized_declarator",
        "abstract_pointer_declarator",
    ):
        child = node.child_by_field_name("declarator")
        if child:
            return _fn_name_from_declarator(child)
    return None


def _walk(node, out: list) -> None:
    """Depth-first traversal; appends (name, start_line, end_line) for each
    function_definition node."""
    if node.type == "function_definition":
        declarator = node.child_by_field_name("declarator")
        name = _fn_name_from_declarator(declarator)
        if name:
            # tree-sitter points are 0-indexed; convert to 1-indexed for humans
            out.append((name, node.start_point[0] + 1, node.end_point[0] + 1))
    for child in node.children:
        _walk(child, out)


# ---------------------------------------------------------------------------
# Public parsing API
# ---------------------------------------------------------------------------

def extract_functions(source: str | bytes) -> list[tuple[str, int, int]]:
    """Parse C source and return [(name, start_line, end_line)] for every
    function definition found.

    Lines are 1-indexed.  Returns an empty list when tree-sitter is not
    installed or the source cannot be parsed — callers degrade gracefully.
    """
    if not TREE_SITTER_AVAILABLE:
        return []
    if isinstance(source, str):
        source = source.encode("utf-8", errors="replace")
    tree = _parser.parse(source)
    results: list[tuple[str, int, int]] = []
    _walk(tree.root_node, results)
    return results


# ---------------------------------------------------------------------------
# Symbol loading (reads from the per-target runs/ directory on the host)
# ---------------------------------------------------------------------------

def load_symbols_for_file(filename: str, target) -> list[str] | None:
    """Return the compiled symbol list for *filename* from the target's
    reachable_symbols.json, or None if the file is unavailable or the
    filename has no entry (e.g. header-only files without a matching .o).

    The reachable_symbols.json is generated during the Docker image build by
    running ``nm --defined-only`` over every .o file and is stored at
    runs/targets/<target_name>/reachable_symbols.json on the host.
    """
    from config import config  # local import to avoid circular dependency

    path = config.reachable_symbols_path(target.name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    # Keys are source basenames (e.g. "minissdp.c"); .get returns None for
    # header files or files not present in the build.
    return data.get(filename)


# ---------------------------------------------------------------------------
# Annotation builder
# ---------------------------------------------------------------------------

def dead_function_annotation(filepath: Path, compiled_symbols: list[str] | None) -> str:
    """Build a prompt annotation listing functions in *filepath* that were NOT
    compiled into the default binary build.

    Returns an empty string (no annotation) when:
      - tree-sitter is not installed
      - compiled_symbols is None (reachable_symbols.json missing or filename
        not present in it — e.g. header files)
      - every function found in the file appears in compiled_symbols

    The annotation is intentionally non-alarmist: it tells the agent to skip
    these functions, not that they are necessarily buggy.
    """
    if not TREE_SITTER_AVAILABLE or compiled_symbols is None:
        return ""

    try:
        source = filepath.read_bytes()
    except OSError:
        return ""

    all_fns = extract_functions(source)
    if not all_fns:
        return ""

    compiled = set(compiled_symbols)
    dead = [(n, s, e) for n, s, e in all_fns if n not in compiled]
    if not dead:
        return ""

    lines = [
        "\nNote: the following functions in this file are NOT present in the compiled "
        "binary (absent from the symbol table — dead code or compiled out via #ifdef). "
        "Do not report findings that are only reachable through these functions:\n",
    ]
    for name, start, end in dead:
        lines.append(f"  - {name}()  [lines {start}–{end}]")
    return "\n".join(lines) + "\n"
