FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps via uv + requirements.txt
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY backend/requirements.txt .
RUN uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python -r requirements.txt

# Copy backend source
COPY backend/ ./backend/

EXPOSE 3998 3999

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:3998/api/health || exit 1

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "backend/main.py"]
