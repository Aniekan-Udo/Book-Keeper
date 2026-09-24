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

# bring the schema up to date, then serve
CMD ["sh", "-c", "uv run python migrate.py && uv run uvicorn main:app --host 0.0.0.0 --port 8001"]