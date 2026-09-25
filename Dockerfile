FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY app ./app
RUN uv sync --locked

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8001
CMD ["python", "-m", "app.main"]
