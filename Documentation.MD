# Auth & Payment Verification Layer — Technical Documentation

## Purpose

This document describes the authentication, session management, step-up
verification, and transfer subsystem for the payment product. It covers
architecture, token lifecycles, database schema, endpoint contracts,
security rationale, and known gaps. Intended audience: engineers working
on or reviewing this codebase.

---

## 1. Architecture Overview

The system has three layers, each with a distinct responsibility:

1. **Session layer** — access token (short-lived) + refresh token
   (longer-lived, DB-backed, rotating). Answers "is this a valid,
   ongoing session for this user."
2. **Step-up layer** — a separate, short-lived token issued only after
   a fresh re-verification (biometric or password). Answers "did this
   user just prove their identity again, specifically for this
   sensitive action."
3. **Transaction layer** — the `/transfer` endpoint and supporting
   tables (`transactions`, `idempotency_records`) that move money
   atomically, once both layers above have cleared.

These layers are intentionally decoupled. A valid session does not
imply step-up verification; step-up verification alone does not imply
a valid session. Sensitive endpoints require both.

---

## 2. Token Types

| Token type   | Lifetime      | Storage (client)         | Storage (server)              | Purpose                              |
|--------------|---------------|---------------------------|--------------------------------|---------------------------------------|
| Access token | ~15–30 min    | Memory / Authorization header | None (stateless JWT)      | General API access                    |
| Refresh token| ~7 days       | `httpOnly` cookie (web) / secure storage (mobile) | `refresh_tokens` table (by `jti`) | Silently renew access tokens; revocable |
| Step-up token| ~2–5 min      | Memory / request header   | `step_up_verifications` table | Gate on sensitive actions (transfer, etc.) |

All tokens are JWTs signed with `SECRET_KEY` (HS256), carrying a `type`
claim (`refresh`, `step_up`) so one type cannot be substituted for
another. Access tokens carry no explicit `type` claim; they're the
default assumed by `verify_token`.

### Why three separate token types instead of one

Collapsing these into a single token (e.g., just using a long-lived
access token everywhere) would mean a leaked token grants full
standing access to sensitive actions indefinitely. Splitting them
means:
- A leaked access token is dangerous only for its short window.
- A leaked refresh token can be revoked without affecting other users
  or requiring a full credential reset.
- Step-up tokens exist specifically so "logged in" and "just verified
  identity for this transaction" are never conflated.

---

## 3. Refresh Token Lifecycle (Rotation + Reuse Detection)

1. Login issues an access token and a refresh token (with a unique
   `jti`), and stores the `jti` in `refresh_tokens`.
2. When the access token expires, the client calls `/refresh` with
   the refresh token.
3. `/refresh` validates the token, checks the `jti` is still valid
   (exists, not revoked, not expired) via `is_refresh_token_valid`.
4. On success: the old `jti` is revoked (`revoke_refresh_token`), and
   a **new** access + refresh token pair is issued and stored. This is
   rotation — every refresh invalidates the token that was just used.
5. **Reuse detection:** if a refresh token is presented whose `jti` is
   *not* valid (already rotated, revoked, or unknown), this is treated
   as a signal of token theft — a legitimate client only ever holds the
   latest token. All of that user's sessions are revoked
   (`revoke_all_user_tokens`), forcing a fresh login everywhere.

This means a stolen-but-unused refresh token is fine (attacker never
gets a chance to use it before the real user's next refresh rotates it
out). A stolen-and-used refresh token immediately kills every session
for that user the moment the real user's client tries to refresh with
its now-invalidated copy.

---

## 4. Step-Up Verification

Sensitive endpoints (`/transfer`) require, in addition to a valid
session, a step-up token obtained via `/verify-step-up` within the
last few minutes.

**Current approach:** native platform biometric attestation —
Apple App Attest (iOS) / Google Play Integrity (Android) — rather than
custom facial recognition. Rationale:

- The OS verifies the biometric locally in secure hardware; the
  server never receives or stores raw biometric data or embeddings.
- Avoids biometric-data compliance obligations (GDPR special category
  data, Illinois BIPA, etc.) that come with storing face/fingerprint
  data ourselves.
- Liveness/spoof detection is handled by Apple/Google's dedicated
  secure hardware rather than something we'd need to build and
  continuously harden ourselves.

**Fallback:** password or OTP re-entry, for devices/users without
biometrics enrolled. (Status: not yet implemented — see Gaps.)

**Status of platform verification:** `verify_platform_assertion` for
Google Play Integrity can be implemented directly against Google's
REST API (server-side verdict, low risk). The Apple App Attest
counterpart requires certificate-chain and nonce verification against
Apple's attestation format — this should be built from Apple's
official reference implementation/sample code, not a from-scratch
implementation, given the consequence of a subtly incorrect
verification routine. **Not yet implemented** — currently deprioritized
in favor of Alembic/logging/rate limiting.

---

## 5. Database Schema

All models in `db.py`, using SQLAlchemy's `Mapped[...]` declarative
style, tracked via Alembic migrations (`migrations/versions/`).

- **`user`** — `id`, `firstname`, `lastname`, `email` (unique),
  `password` (Argon2 hash, `String(255)`), `balance`
  (`DECIMAL(10,2)`).
- **`refresh_tokens`** — `id`, `user_id`, `jti` (unique), `expires_at`
  (`DateTime(timezone=True)`), `revoked` (`Boolean`).
- **`step_up_verifications`** — `id`, `user_id`, `method`
  (`"password"` / `"biometric"` / `"otp"`), `expires_at`.
- **`transactions`** — immutable ledger. `id`, `reference` (public-safe
  UUID-based ID), `sender_id`, `recipient_id`, `amount`,
  `sender_balance_after`, `recipient_balance_after`, `status`
  (`completed` / `failed` / `reversed`), `idempotency_key`,
  `created_at`. Rows are never edited after creation — a reversal is a
  *new* row referencing the original, not a mutation.
- **`idempotency_records`** — `id`, `key` (unique), `user_id`,
  `response` (JSON — the exact response returned on the original
  request, replayed verbatim on retry), `created_at`.

All money fields use `DECIMAL`, never `float` — required for correct
arithmetic on monetary values.

---

## 6. Endpoints

| Endpoint | Method | Auth required | Notes |
|---|---|---|---|
| `/login` | POST | None | Verifies password hash, issues access + refresh token, sets refresh cookie |
| `/refresh` | POST | Refresh token (cookie) | Rotates refresh token, issues new access token |
| `/logout` | POST | Refresh token (cookie) | Revokes the current session's refresh token |
| `/verify-step-up` | POST | Access token | Verifies platform biometric assertion, issues step-up token |
| `/transfer` | POST | Access token + step-up token | Row-locked balance mutation, ledger entry, idempotent |
| `/check_balance` | POST | Access token | To be migrated off request-body identity — see Gaps |
| `/generate_report` | POST | — | Uses Redis distributed lock (different problem class than DB row locking — see §7) |
| `/health` | GET | None | Liveness check |

---

## 7. Locking Strategy — DB Row Locks vs. Redis Distributed Lock

Two different locking mechanisms are used deliberately for two
different problem shapes:

- **DB row-level locking (`with_for_update()`, inside `db.begin()`)**
  — used in `/transfer`. Correctness lives entirely inside one atomic
  DB transaction (read balance, check, write balance). Postgres's own
  lock is atomic with the write, auto-releases on crash/rollback, and
  doesn't require Redis to be available.
- **Redis distributed lock (`cache.add` as mutex)** — used in
  `/generate_report`. The protected work is expensive application-level
  computation with no natural DB transaction to anchor a lock to;
  Redis coordinates across processes for that case.

**Do not use a Redis lock where a DB transaction already provides
the guarantee** — it adds a second point of failure without adding
correctness.

**Lock ordering in `/transfer`:** currently locks sender then
recipient in request order. This is a known gap — should lock by a
stable order (e.g., always lower `user.id` first) to prevent deadlocks
between two transfers in opposite directions happening concurrently.
**Not yet fixed.**

---

## 8. Idempotency

`/transfer` requires an `idempotency_key` from the client. The DB
(not Redis) is used to store idempotency records, specifically so the
idempotency record commits atomically with the balance mutation and
ledger entry in the same transaction — preserving a complete audit
trail and eliminating the crash-window gap that would exist with a
separately-written Redis key. Redis-based idempotency remains
appropriate for lower-stakes, non-monetary endpoints.

---

## 9. Environment & Secrets

- Config loaded via `pydantic-settings` (`authentication/settings.py`),
  reading from `.env`, resolved relative to the settings file's own
  location (not the process's working directory).
- `SECRET_KEY` is a `SecretStr` — masked in logs/reprs; access via
  `.get_secret_value()` only at the point of use (JWT signing).
- `.env` must never be committed. Local dev and production must use
  different `SECRET_KEY` values. Production should source secrets from
  a secrets manager, not a flat file.
- `extra="ignore"` is set on `Settings` since `.env` is shared with
  non-auth config (Postgres credentials); consider splitting into
  per-concern settings classes (`AuthSettings`, `DBSettings`) as the
  config surface grows.

---

## 10. Migrations

Schema is managed via Alembic (`migrations/`), using an async-aware
`env.py` (standard sync template doesn't work with the `asyncpg`
engine — requires `async_engine_from_config` +
`connection.run_sync(...)`, see `migrations/env.py`).

Standard flow for a schema change:
```bash
alembic revision --autogenerate -m "description of change"
# review the generated file in migrations/versions/ before applying —
# autogenerate is not infallible, especially around enums
alembic upgrade head
```

---

## 11. Known Gaps / Not Yet Implemented

- `verify_platform_assertion` (Apple App Attest / Google Play
  Integrity) — Play Integrity is low-risk to implement directly;
  App Attest should be built from Apple's reference implementation.
- Step-up fallback path (password/OTP) when biometrics unavailable.
- Consistent lock ordering in `/transfer` (currently request-order,
  should be ID-order) to prevent deadlocks.
- Rate limiting (planned: `slowapi`, Redis-backed, per-IP on
  `/login`/`/refresh`, per-user on `/transfer`).
- Structured logging (planned: `structlog`, JSON output, request-ID
  correlation, PII/secret redaction) — currently uses ad hoc `print()`.
- `/check_balance` and other early endpoints still need migrating to
  use `get_current_user` instead of accepting identity fields in the
  request body.
- Full test suite (unit tests for token functions exist in progress;
  integration tests for `/transfer`, `/refresh` reuse detection, and
  concurrent-transfer locking behavior not yet written).
- `GET /transactions` — user-facing transaction history endpoint.
- KYC / legal identity verification — out of scope for the current
  App Attest / Play Integrity work, which only confirms device/
  biometric integrity, not legal identity. Would require a separate
  third-party KYC integration (e.g., Smile ID, Persona) if/when needed
  for compliance.

---

## 12. Security Review Notes

Given this system handles real transfers, the following should get
explicit review before wider rollout, beyond normal code review:
- `/transfer` locking and idempotency logic.
- Whichever platform-assertion verification code is eventually
  written (especially App Attest's certificate/nonce validation).
- Refresh token rotation and reuse-detection logic.