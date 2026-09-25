FROM python:3.11-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy the rest of the app
COPY . .

EXPOSE 8001

# bring the schema up to date, then serve. --no-sync: dependencies were installed above;
# don't let `uv run` add dev packages at startup. --proxy-headers: behind Caddy, take the
# client's real IP from X-Forwarded-For (for per-IP rate limits) — only trusted because
# the app's port is never published to the internet (see docker-compose.yml).
CMD ["sh", "-c", "uv run --no-sync python migrate.py && uv run --no-sync uvicorn main:app --host 0.0.0.0 --port 8001 --proxy-headers --forwarded-allow-ips='*'"]
