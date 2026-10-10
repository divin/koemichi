FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

RUN mkdir -p /data && chown -R 1000:1000 /data
ENV PATH="/app/.venv/bin:$PATH"
ENV DATA_DIR=/data
USER 1000:1000

EXPOSE 8000
CMD ["uvicorn", "koemichi.services.intake.main:app", "--host", "0.0.0.0", "--port", "8000"]
