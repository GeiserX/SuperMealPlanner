FROM python:3.14.7-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY supermealplanner ./supermealplanner
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH SMP_DATA=/data HOME=/tmp
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["supermealplanner", "serve", "--port", "8080"]
