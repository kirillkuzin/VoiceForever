# syntax=docker/dockerfile:1

# Build stage. aiogram allows only pydantic < 2.14, and its pydantic-core has no
# prebuilt wheels for Python 3.15, so it is compiled here (needs Rust >= 1.88;
# Debian's rustc is older, hence rustup).
FROM python:3.15-slim AS build
COPY --from=astral/uv:0.13 /uv /uvx /bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
       | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY vf_server ./vf_server
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Runtime stage: Python and the virtualenv only, no compilers.
FROM python:3.15-slim
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1
RUN useradd --create-home vf && mkdir -p /data && chown vf /data
COPY --from=build /app/.venv /app/.venv
WORKDIR /app
USER vf
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('VF_WS_PORT', '8765'), timeout=3)"
CMD ["voiceforever-server"]
