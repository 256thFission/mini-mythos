FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    gcc \
    clang \
    make \
    cmake \
    gdb \
    git \
    python3 \
    python3-pip \
    curl \
    libnftnl-dev \
    libmnl-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone miniupnp and pin to the exact target hash in one atomic step so the
# Docker layer cache never holds a stale clone at the wrong HEAD.
RUN git clone https://github.com/miniupnp/miniupnp.git /opt/miniupnp \
    && git -C /opt/miniupnp checkout f83b5e2e21aa8dfa393ff80ea287ac4fca1a4df1

WORKDIR /opt/miniupnp

# Build miniupnpd with ASan + UBSan instrumentation
WORKDIR /opt/miniupnp/miniupnpd
RUN sh configure --leasefile --vendorcfg
RUN make -f Makefile.linux_nft CC=clang \
    CFLAGS="-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined" \
    LDFLAGS="-fsanitize=address,undefined -Wl,-z,now -Wl,-z,relro -pie" \
    LDLIBS="-lnftnl -lmnl"

# Extract per-file live symbols for dead-code filtering
# NOTE: build context is the repo root — see README (docker build -f docker/Dockerfile .)
COPY docker/extract_symbols.py /tmp/extract_symbols.py
RUN python3 /tmp/extract_symbols.py

# Install Node.js and claude CLI so agents run natively inside the container
RUN apt-get update && apt-get install -y nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

# ── Harness tools (MCP servers, etc.) ─────────────────────────────────────────
# The submit MCP stdio server exposes submit_audit_report / submit_judge_verdict
# as real tools for the audit + judge agents (registered via --mcp-config).
COPY tools/ /opt/minimythos/tools/
RUN chmod 0755 /opt/minimythos/tools/*.py

# Create non-root audit user — claude refuses --dangerously-skip-permissions as root
RUN useradd -m -s /bin/bash audit \
    && mkdir -p /audit-home \
    && chown -R audit:audit /audit-home \
    && chown -R audit:audit /opt/miniupnp \
    && chown -R audit:audit /opt/minimythos

# Keep container alive for docker exec
CMD ["tail", "-f", "/dev/null"]
