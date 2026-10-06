# syntax=docker/dockerfile:1.7
# Multi-stage build:
#   runtime -> slim production image (no dev deps, no tests), non-root, healthcheck
#   dev     -> runtime + dev deps + tests, used by docker compose so that
#              `docker compose exec agent pytest tests/` works as documented

ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS uv-base
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir uv==0.12.23
WORKDIR /app
COPY pyproject.toml uv.lock ./

# Both venvs are built at the same path: console-script shebangs embed it.
FROM uv-base AS deps-runtime
RUN uv sync --frozen --no-dev --no-install-project

FROM uv-base AS deps-dev
RUN uv sync --frozen --group dev --no-install-project

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 --create-home app
WORKDIR /app
COPY --from=deps-runtime /opt/venv /opt/venv
COPY alembic.ini ./
COPY src ./src
RUN mkdir -p logs && chown app:app logs
USER app
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory src.api.app:app_factory --host 0.0.0.0 --port 8080 --no-access-log"]

FROM runtime AS dev
USER root
COPY --from=deps-dev /opt/venv /opt/venv
COPY pyproject.toml uv.lock ./
COPY tests ./tests
COPY quote-service ./quote-service
COPY scripts ./scripts
RUN chown -R app:app /app
ENV PYTEST_ADDOPTS="-p no:cacheprovider" HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis
USER app
