# MiniMythos — auto-generated from targets/dropbearssh/target.toml
# DO NOT EDIT BY HAND. Re-render via:  python3 -m harness.setup_cli setup dropbearssh
# (If you need custom build steps the template can't express, delete this file
# and write a hand-crafted Dockerfile at the same path — setup will use that.)

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# Base toolchain + anything the target-specific build needs.
RUN apt-get update && apt-get install -y \
    gcc clang make cmake gdb git python3 python3-pip curl nodejs npm \
zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI — agents run natively inside the container.
RUN npm install -g @anthropic-ai/claude-code

# Python deps for the submit MCP stdio server (tools/submit_mcp_server.py).
# --break-system-packages is safe here: this image is single-purpose.
RUN pip install --no-cache-dir --break-system-packages mcp jsonschema

# Clone + pin in a single layer so the cache never holds a stale HEAD.
RUN git clone https://github.com/mkj/dropbear.git /opt/dropbear \
    && git -C /opt/dropbear checkout c60db6408178aaca0a8aacd017ac344a2cfb30c8

WORKDIR /opt/dropbear

# ── Build steps (from [build].commands in target.toml) ────────────────────────
RUN ./configure
RUN make CC=clang MORECFLAGS='-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined' MORELDFLAGS='-fsanitize=address,undefined'

# ── Harness tools (MCP submit server) baked in ────────────────────────────────
COPY tools/ /opt/minimythos/tools/
RUN chmod 0755 /opt/minimythos/tools/*.py

# Non-root audit user — claude refuses --dangerously-skip-permissions as root.
RUN useradd -m -s /bin/bash audit \
    && mkdir -p /audit-home \
    && chown -R audit:audit /audit-home \
    && chown -R audit:audit /opt/dropbear \
    && chown -R audit:audit /opt/minimythos

# Lock *existing files* in the build tree read-only so the audit agent cannot
# patch the Makefile, edit source, or overwrite cached .o files (which is what
# any `make` rebuild would do first). Directories stay writable so running the
# instrumented binaries can still create runtime artifacts inside their own
# tree (hostkeys, lease files, PID files, unix sockets, logs). Scratch work
# for the agent's own test programs still belongs under $HOME (/audit-home).
RUN find /opt/dropbear -type f -exec chmod a-w {} +

CMD ["tail", "-f", "/dev/null"]
