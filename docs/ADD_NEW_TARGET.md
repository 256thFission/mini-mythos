# Adding a new target to MiniMythos

Two steps:

1. Write a `targets/<name>/target.toml` (schema below).
2. Run `python3 harness/setup_cli.py setup <name>`.

That's it. The setup CLI renders a Dockerfile from `docker/Dockerfile.tmpl`,
builds the image, starts the container, and copies `reachable_symbols.json`
to `runs/targets/<name>/`. After that, `python3 -u harness/orchestrator.py
--target <name>` runs the audit.

If your project is too unusual for the template (custom base image, multi-stage
build, pre-build patches, etc.), drop a hand-written `targets/<name>/Dockerfile`
in place — the setup CLI will use that instead.

---

## Schema

```toml
[project]
name = "<name>"                # must match the directory name under targets/
description = "short description"

[build]
repo_url = "https://github.com/owner/repo.git"
repo_revision = "<full sha>"   # pin — a tag or branch drifts
workdir = "/opt/<name>"        # where the repo is cloned inside the container
build_dir = "."                # subdir of workdir where commands run (often "." or "src")

apt_packages = []              # extra Debian packages beyond the base image
                               # base already includes: gcc clang make cmake gdb
                               # git python3 python3-pip curl nodejs npm

commands = [                   # one RUN per element, executed in order
    "./configure",
    "make CC=clang CFLAGS='-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined' LDFLAGS='-fsanitize=address,undefined'",
]

[symbols]
# Glob for compiled .o files, relative to the build_dir. The extractor maps
# each object → source by basename lookup, so names must be unique across
# included subdirs (collisions are silently merged).
object_glob = "*.o"            # autotools flat: "*.o" — CMake: "build/**/*.o"
source_exts = [".c"]           # add ".cc"/".cpp"/".cxx" for C++ projects
```

---

## Cursor prompt — "Given a GitHub URL, fill in target.toml"

Copy the block below into Cursor chat after you've cloned the repo or looked
at it on GitHub. Replace `<URL>` and `<NAME>` before sending.

> You're adding a new MiniMythos target for `<URL>` under the name `<NAME>`.
> Read `docs/ADD_NEW_TARGET.md` for the schema and `targets/dropbearssh/target.toml`
> and `targets/miniupnpd/target.toml` as examples.
>
> Create `targets/<NAME>/target.toml` by:
> 1. Finding the project's current HEAD commit on the default branch and pinning it in `repo_revision`.
> 2. Inspecting the build system: autotools (`configure.ac`), CMake (`CMakeLists.txt`), or plain Makefile. Set `commands` accordingly with ASan + UBSan:
>    - Autotools: `./configure` then `make CC=clang CFLAGS='-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined' LDFLAGS='-fsanitize=address,undefined'`.
>    - If the project's Makefile hard-codes CFLAGS and breaks on override (libtommath in dropbear is the canonical example), use `MORECFLAGS`/`MORELDFLAGS` or any append-only knob the project exposes.
>    - CMake: `cmake -B build -DCMAKE_C_COMPILER=clang -DCMAKE_C_FLAGS='-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined' -DCMAKE_EXE_LINKER_FLAGS='-fsanitize=address,undefined'` then `cmake --build build -j$(nproc)`. Set `symbols.object_glob = "build/**/*.o"`.
> 3. Adding required build packages to `apt_packages` (grep `configure.ac` / `CMakeLists.txt` / `README` for `pkg-config`/`find_package`/`apt install` hints).
> 4. Setting `symbols.source_exts` to include `.cc`, `.cpp`, or `.cxx` if the project is C++.
>
> Do NOT write a Dockerfile — the setup CLI will render one. After writing
> the TOML, tell me to run `python3 harness/setup_cli.py setup <NAME>` and
> I'll iterate on build errors with you.
