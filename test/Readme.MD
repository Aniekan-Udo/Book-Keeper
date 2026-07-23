# Test Suite — Setup Notes

## Install

```bash
uv add --dev pytest pytest-asyncio httpx
```

## Files

- `conftest.py` — shared fixtures: test DB engine/session (rolled back
  per test), `test_user`, `test_recipient`.
- `test_auth_tokens.py` — pure unit tests, no DB: password hashing,
  access/refresh/step-up token creation and verification.
- `test_refresh_token_storage.py` — DB-backed tests: storing,
  validating, revoking, and rotating refresh tokens; reuse detection.
- `test_transfer_endpoint.py` — integration tests: successful
  transfer, insufficient balance, missing recipient, idempotency
  (duplicate request), and a concurrency test firing two simultaneous
  transfers to confirm the row lock prevents a lost-update race.
- `test_login_refresh_logout.py` — integration tests for the full
  session lifecycle: login, refresh, reuse detection at the HTTP
  layer, logout.

## Required adaptation before running

These tests were written against the functions and models built over
the course of this project's design conversation, but a few names are
**assumed** and need to match your actual file layout:

1. **`from main import app`** — update to wherever your `FastAPI()`
   instance actually lives (e.g. `from app import app`, or whatever
   your entrypoint module is called).
2. **`from authentication.auth import get_current_user, require_step_up`**
   in `test_transfer_endpoint.py` — update the import path if these
   dependency functions live somewhere else.
3. **`TEST_DATABASE_URL`** in `conftest.py` — defaults to
   `postgresql+asyncpg://test:test@localhost:5434/test_db`. Set this
   env var to point at a real, separate test database — **do not**
   point it at your dev or prod database, since the test DB's tables
   are created and dropped automatically each session.

## Running

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://test:test@localhost:5434/test_db"
uv run pytest test/ -v
```

Move `pytest.ini` to your project root (not inside `test/`) if pytest
doesn't pick it up automatically from this location.

## What's intentionally NOT covered yet

- `verify_platform_assertion` (App Attest / Play Integrity) — not
  implemented in the app yet, so there's nothing to test.
- Rate limiting — add tests once `slowapi` is wired in.
- `/verify-step-up` endpoint itself (as opposed to the `require_step_up`
  dependency, which is stubbed via override in the transfer tests) —
  add once the platform-assertion verification is implemented, since
  testing it meaningfully needs either mocked Apple/Google responses
  or a documented "always succeeds" test mode.
- Load/stress testing beyond the two-concurrent-requests case in
  `test_concurrent_transfers_do_not_corrupt_balance` — that test
  proves the lock works, not that it holds up under real load; a
  separate load test (e.g. with `locust`) would be the next step for
  that.