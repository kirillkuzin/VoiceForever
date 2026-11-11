FROM python:3.15-slim
COPY --from=astral/uv:0.13 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app
# Dependencies first (cached layer), exact versions from uv.lock
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY vf_server ./vf_server
RUN uv sync --locked --no-dev \
    && useradd --create-home vf && mkdir -p /data && chown vf /data
USER vf
VOLUME ["/data"]
EXPOSE 8765
CMD ["/app/.venv/bin/voiceforever-server"]
