FROM ubuntu:latest

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
        python3-pip \
        python3-venv \
        gnupg \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Use Ubuntu's default non-root user ('ubuntu', UID 1000) to install and run
# Claude Code as -- no custom user needed.
ENV HOME=/home/ubuntu

# Install Claude Code via the official native installer, as the user (not root),
# so the binary lands at ~/.local/bin/claude owned by a real account -- matching a
# normal native install rather than a root-owned one.
USER ubuntu
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="${HOME}/.local/bin:${PATH}"

# Pre-create ~/.claude (owned by this user) so bind-mounting individual config
# files into it keeps the dir writable for Claude's runtime state (projects,
# todos, ...) instead of Docker creating it root-owned.
RUN mkdir -p "$HOME/.claude"

WORKDIR /work
ENTRYPOINT ["claude"]
