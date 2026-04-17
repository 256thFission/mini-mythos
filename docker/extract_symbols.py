"""Extract per-file compiled symbols → reachable_symbols.json.

Captures both global (T) and local/static (t) text-section symbols so the
dead-function annotation in preprocessor.py can correctly identify static
functions that are compiled but not exported.

Usage (inside a target's Dockerfile):
    COPY docker/extract_symbols.py /usr/local/bin/extract_symbols.py
    RUN python3 /usr/local/bin/extract_symbols.py /opt/<project> \\
        --object-glob '*.o' --source-exts '.c'

Notes:
- ``workdir`` is the directory to scan and where ``reachable_symbols.json``
  is written (the orchestrator copies it out from here).
- ``--object-glob`` is a Python glob (supports ``**``) relative to ``workdir``.
  Use ``*.o`` for flat autotools projects, ``build/**/*.o`` for CMake.
- ``--source-exts`` is a comma-separated list of candidate source extensions.
  The extractor tries each in order to recover the source filename from an
  object filename (strips ``.o`` then appends). Defaults to ``.c`` only.
- Name collisions (two ``util.c`` in different subdirs) are *merged* into one
  entry; this is acceptable for dead-code filtering.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path


def _source_name(obj_path: str, source_exts: list[str]) -> str:
    """Map an object path → a source filename (basename + one candidate ext).

    CMake emits ``foo.c.o`` (keeping the original extension); autotools emits
    ``foo.o``. Handle both.
    """
    name = Path(obj_path).name  # strip directory
    # CMake: foo.c.o → foo.c already has the source ext
    stem_once = name[: -len('.o')] if name.endswith('.o') else name
    for ext in source_exts:
        if stem_once.endswith(ext):
            return stem_once  # already has a known source ext (CMake style)
    # Autotools: foo.o → try foo + each ext, pick first that exists on disk
    base = stem_once
    for ext in source_exts:
        if list(glob.iglob(f'**/{base}{ext}', recursive=True)):
            return base + ext
    # Fall back to the first configured extension
    return base + source_exts[0]


def extract(workdir: str, object_glob: str, source_exts: list[str]) -> None:
    os.chdir(workdir)
    objects = sorted(glob.glob(object_glob, recursive=True))
    if not objects:
        print(
            f'[extract_symbols] WARNING: no objects matched {object_glob!r} '
            f'under {workdir}. Writing empty map.',
            file=sys.stderr,
        )
    result: dict[str, list[str]] = {}
    for obj in objects:
        src = _source_name(obj, source_exts)
        out = subprocess.run(
            ['nm', '--defined-only', obj],
            capture_output=True, text=True,
        )
        funcs = [
            parts[-1]
            for line in out.stdout.splitlines()
            for parts in [line.split()]
            if len(parts) >= 3 and parts[1] in ('T', 't')
        ]
        result.setdefault(src, []).extend(funcs)

    with open('reachable_symbols.json', 'w') as f:
        json.dump(result, f, indent=2)
    total = sum(len(v) for v in result.values())
    print(
        f'[extract_symbols] Wrote {workdir}/reachable_symbols.json: '
        f'{len(result)} files, {total} symbols (from {len(objects)} objects)'
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('workdir', help='Directory to scan and write output into')
    p.add_argument(
        '--object-glob', default='*.o',
        help='Glob for object files (relative to workdir). Default: *.o',
    )
    p.add_argument(
        '--source-exts', default='.c',
        help='Comma-separated candidate source extensions. Default: .c',
    )
    args = p.parse_args()
    exts = [e.strip() for e in args.source_exts.split(',') if e.strip()]
    extract(args.workdir, args.object_glob, exts)


if __name__ == '__main__':
    main()
