# Install the locked dependencies once; no host Python or virtualenv is needed.
FROM python:3.14-slim AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN useradd --create-home --uid 10001 nexa
COPY --from=dependencies /app/.venv /app/.venv
COPY app ./app
COPY alembic ./alembic
COPY scripts ./scripts
COPY alembic.ini pyproject.toml uv.lock README.md ./
USER nexa
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM dependencies AS test-dependencies
RUN uv sync --locked --no-install-project

FROM runtime AS test
COPY --from=test-dependencies /app/.venv /app/.venv
COPY tests ./tests
CMD ["python", "-m", "pytest", "tests", "-v", "-o", "cache_dir=/tmp/pytest-cache"]
