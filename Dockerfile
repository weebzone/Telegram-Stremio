FROM ghcr.io/astral-sh/uv:debian-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV MALLOC_ARENA_MAX=2
ENV LANG=en_US.UTF-8
ENV PATH="/app/.venv/bin:$PATH"
# `uv run` in start.sh must not re-resolve or re-install on every boot
ENV UV_FROZEN=1
ENV UV_NO_DEV=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
	bash \
        git \
        curl \
        ca-certificates \
        locales && \
    locale-gen en_US.UTF-8 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first so code-only changes reuse this layer (and a 1 GB host does not reinstall every build)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN chmod +x start.sh
CMD ["bash", "start.sh"]
