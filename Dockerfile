FROM python:3.11-slim

WORKDIR /app

# Take Debian's security fixes published since the base image was built (rebuild regularly:
# fixes for the rest arrive the same way), and remove the system Python's pip, setuptools
# and wheel: uv installs everything and the app runs from uv's virtualenv, so they're unused
# code (pip bundles its own copies of urllib3, msgpack…) that scanners rightly flag.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*     && python -m pip uninstall -y pip setuptools wheel

# Install uv (pinned: a new upstream release can't change the build unnoticed)
COPY --from=ghcr.io/astral-sh/uv:0.10.3 /uv /uvx /bin/

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy the rest of the app
COPY . .

# Run as an unprivileged user, not root: if the app were ever compromised, the attacker
# couldn't modify the code, the installed packages or the system. Nothing under /app needs
# writing at runtime (no bytecode cache; uv doesn't sync at startup).
ENV PYTHONDONTWRITEBYTECODE=1 UV_NO_CACHE=1
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8001

# bring the schema up to date, then serve. --no-sync: dependencies were installed above;
# don't let `uv run` add dev packages at startup. --proxy-headers: behind Caddy, take the
# client's real IP from X-Forwarded-For (for per-IP rate limits) — only trusted because
# the app's port is never published to the internet (see docker-compose.yml).
# --no-server-header: don't advertise the server software and version. PORT: hosts like
# Render choose the port; 8001 otherwise. SEED_DEMO=true: create the demo accounts on start
# if they aren't there (hosts whose database can't be reached from outside); a failed seed
# is logged and the app still starts.
CMD ["sh", "-c", "uv run --no-sync python migrate.py && { [ \"$SEED_DEMO\" != true ] || uv run --no-sync python -m scripts.seed_demo --if-missing || echo 'Demo seed failed, starting anyway'; } && uv run --no-sync uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001} --proxy-headers --forwarded-allow-ips='*' --no-server-header"]
