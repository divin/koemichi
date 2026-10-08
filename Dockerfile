FROM python:3.12-slim

# tzdata provides system timezone data; ffmpeg converts uploaded audio to WAV.
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Install uv from the official image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install the locked production dependencies.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# Audio files and SQLite data are stored on the /data volume.
RUN mkdir -p /data/memos /data/actions && \
    chown -R 1000:1000 /data

ENV PATH="/app/.venv/bin:$PATH"
ENV DATA_DIR=/data

USER 1000:1000

EXPOSE 8000

CMD ["uvicorn", "koemichi.services.ingest.main:app", "--host", "0.0.0.0", "--port", "8000"]
