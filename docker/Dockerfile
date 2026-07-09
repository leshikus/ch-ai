FROM debian:bookworm-slim

# Tools commonly needed inside the agent sandbox.
# gh is installed from GitHub's official apt repo (cli.github.com).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        ripgrep \
        less \
        openssh-client \
        python3 \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# A normal non-root user to install and run Claude Code as.
RUN useradd --create-home --home-dir /home/agent --shell /bin/bash --uid 1000 agent
ENV HOME=/home/agent

# Install Claude Code via the official native installer, as the user (not root),
# so the binary lands at ~/.local/bin/claude owned by a real account -- matching a
# normal native install rather than a root-owned one.
USER agent
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/agent/.local/bin:${PATH}"

# Make HOME world-writable so the container can also run as the host's UID:GID
# (passed via `docker run --user`) and still write config and let the native
# auto-updater drop new versions into ~/.local/share/claude.
USER root
RUN chmod -R 0777 /home/agent

USER agent
WORKDIR /work
ENTRYPOINT ["claude"]
