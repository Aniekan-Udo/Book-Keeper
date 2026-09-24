# Test Suite

## Running

```bash
uv run pytest                 # everything, on a throwaway SQLite file — no services needed
uv run pytest -k ledger       # just the ledger rules
```

To run against Postgres (closer to production — e.g. real row locks and timezone
handling), point `TEST_DATABASE_URL` at a **separate** test database. Its tables are
dropped and recreated, so never point it at dev or prod data:

```bash
TEST_DATABASE_URL="postgresql+asyncpg://test:test@localhost:5434/test_db" uv run pytest
```

## Files

- `conftest.py` — test DB engine/session (each test rolls back), `test_user`, `other_user`.
- `test_auth_tokens.py` — password hashing and JWT creation/verification, no DB.
- `test_refresh_token_storage.py` — storing, validating, revoking and rotating refresh tokens.
- `test_login_refresh_logout.py` — the session lifecycle over HTTP.
- `test_bookkeeping_api.py` — registration, and that bookkeeping endpoints only ever act
  for the user in the access token (LLM stubbed).
- `../tools/test/test_ledger.py` — the money rules: debt settlement, loans, reports,
  undo, corrections.

The LLM itself is not exercised here. For that, run the live evals:
`uv run python -m tools.evals` (see `tools/evals.py`).
