"""Extract per-file compiled symbols from object files and write reachable_symbols.json.

Captures both global (T) and local/static (t) text-section symbols so the
dead-function annotation in preprocessor.py can correctly identify static
functions that are compiled but not exported.
"""
import json
import subprocess
import glob
import os

os.chdir('/opt/miniupnp/miniupnpd')

result = {}
for obj in sorted(glob.glob('*.o')):
    src = obj.replace('.o', '.c')
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
    result[src] = funcs

json.dump(result, open('reachable_symbols.json', 'w'), indent=2)
print(f'Wrote reachable_symbols.json: {len(result)} files, '
      f'{sum(len(v) for v in result.values())} symbols')
