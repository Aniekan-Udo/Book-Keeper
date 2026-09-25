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

- `conftest.py` — test DB session (each test rolls back), a production-like `prod_session`,
  `new_browser` (separate logged-in browsers, each with its own cookies), `test_user`,
  and a guard that fails any test making a real network call.
- `test_sessions.py` — passwords, server-side sessions (login, logout, expiry, instant
  revocation, fixation), and the cross-site request (CSRF) check.
- `test_bookkeeping_api.py` — registration, and that bookkeeping endpoints only ever act
  for the logged-in user (LLM stubbed).
- `test_whatsapp*.py`, `test_voice_notes.py`, `test_debt_collection.py`, `test_statements.py`,
  `test_bank_alerts.py`, `test_whatsapp_signup.py` — the WhatsApp channel and features.
- `../tools/test/test_ledger.py` — the money rules: debt settlement, loans, reports,
  undo, corrections.

The LLM itself is not exercised here. For that, run the live evals:
`uv run python -m tools.evals` (see `tools/evals.py`).
