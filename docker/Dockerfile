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

# Clone miniupnp at the target hash
RUN git clone https://github.com/miniupnp/miniupnp.git /opt/miniupnp

WORKDIR /opt/miniupnp
RUN git checkout f83b5e2

# Build miniupnpd with ASan + UBSan instrumentation
WORKDIR /opt/miniupnp/miniupnpd
RUN sh genconfig.sh --leasefile --vendorcfg
RUN make -f Makefile.linux_nft CC=clang \
    CFLAGS="-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined" \
    LDFLAGS="-fsanitize=address,undefined -Wl,-z,now -Wl,-z,relro -pie" \
    LDLIBS="-lnftnl -lmnl"

# Extract per-file live symbols for dead-code filtering
RUN python3 -c "\
import json, subprocess, glob, os;\
os.chdir('/opt/miniupnp/miniupnpd');\
result = {};\
for obj in sorted(glob.glob('*.o')):\
    src = obj.replace('.o', '.c');\
    out = subprocess.run(['nm', '-g', '--defined-only', obj], capture_output=True, text=True);\
    funcs = [l.split()[-1] for l in out.stdout.splitlines() if ' T ' in l];\
    result[src] = funcs;\
json.dump(result, open('reachable_symbols.json','w'), indent=2);\
print(f'Wrote reachable_symbols.json: {len(result)} files, {sum(len(v) for v in result.values())} symbols')"

# Install Node.js and claude CLI so agents run natively inside the container
RUN apt-get update && apt-get install -y nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

# Create non-root audit user — claude refuses --dangerously-skip-permissions as root
RUN useradd -m -s /bin/bash audit \
    && mkdir -p /audit-home \
    && chown -R audit:audit /audit-home \
    && chown -R audit:audit /opt/miniupnp

# Keep container alive for docker exec
CMD ["tail", "-f", "/dev/null"]
