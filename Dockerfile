# The box. Build from this skep checkout:
#
#   docker build -f Dockerfile -t ghcr.io/anmolnoor/skep .
#
# or `make image`.
#
# One runtime stage. A node stage does not exist at all (the UI is no-build
# static files, RFC decision 1).

FROM python:3.12-slim

# git: worktrees + repo clones. bubblewrap: Linux sandbox. gh: the PR action (U1 land).
# curl: gh's apt repo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git bubblewrap curl ca-certificates \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) \
        signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
        https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /opt/skep
COPY . .

RUN uv sync --frozen --no-dev

# Everything skep writes lives under SKEP_HOME -> one disposable volume (D2).
ENV PATH="/opt/skep/.venv/bin:${PATH}" \
    SKEP_HOME=/data/skep

VOLUME /data
EXPOSE 8765

ENTRYPOINT ["skep", "serve", "--host", "0.0.0.0", "--port", "8765"]
