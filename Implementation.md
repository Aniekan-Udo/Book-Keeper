Good instinct — this is exactly the kind of project that forces you to confront concurrency and distributed-locking issues for real, which lines up with what you've been digging into lately. But before adding more endpoints, there are some foundational issues in what you have that will get worse (and harder to fix) once you layer auth and more money-moving operations on top. Worth fixing these first rather than building on top of them.

**1. Identity vs. authentication — this is the big one**

Right now `check_balance` identifies "who" is asking by trusting `firstname + lastname + email` in the request body. Anyone can query anyone else's balance just by knowing their name and email. For a payment system, this needs to flip completely: the identity should come from a verified token (JWT/session), never from client-supplied fields. Once you add auth, `UserIn` shouldn't exist for these lookups at all — you'd get the user from `Depends(get_current_user)` off the token, and request bodies only carry the *action* (amount, recipient, etc.), not who the actor is.

**2. Money as `float`**

`float(get_user.balance)` will bite you the moment you do arithmetic on it (rounding errors compound in ledgers). Use `Decimal` in the model and keep balances as integer minor units (cents) in the DB, or a numeric/decimal column end-to-end. This is a "fix before it spreads to five more endpoints" issue.

**3. The distributed lock has a release race condition**

You generate `lock_value` but never check it before deleting:

```python
finally:
    await cache.delete(lock_key)
   ```

If your work takes longer than the lock's TTL (60s), the lock could expire, another process acquires it, and then *your* `finally` block deletes *their* lock. The fix is a compare-and-delete (Lua script or `if await cache.get(lock_key) == lock_value: delete`), not a bare delete. This exact bug is worth understanding deeply since it's a classic distributed-systems gotcha — happy to walk through the fix with you rather than just hand you the script, if you want to write it yourself.

**4. Caching mutable financial state is risky**

Caching `check_balance` for 60s means a user (or worse, an authorization check elsewhere) could see a stale balance right after a transaction. For read-mostly, non-critical data this pattern is fine — for balance, you generally want cache invalidation tied to the write path (invalidate on transfer/deposit/withdraw) rather than a blind TTL.

**5. No idempotency keys**

Once you add `transfer` / `withdraw` / `deposit` endpoints, network retries or double-clicks will double-execute unless each request carries a client-generated idempotency key that you store and check before processing. This is a must-have for anything that moves money, not a nice-to-have.

**6. Swallowing exceptions into the response**

`raise HTTPException(status_code=500, detail=str(e))` leaks internal errors (stack traces, DB details) straight to the client — fine for local dev, not for anything touching payments. Log the exception internally, return a generic message.

**7. Minor code smell**

`from sqlalchemy.ext.asyncio import AsyncSession, result` — importing `result` there is unusual and gets shadowed by your local `result = await db.execute(...)` in both functions anyway. Worth double-checking that import's actually needed.

---

**Suggested order if you want to scope this as a real project:**
1. Auth (JWT-based, `get_current_user` dependency) — this reshapes every other endpoint, so do it first.
2. Fix the lock compare-and-delete + move balances to `Decimal`.
3. Add `deposit`/`withdraw`/`transfer` with row-level DB locking (`SELECT ... FOR UPDATE`) for the actual balance mutation — the Redis lock protects your report-generation cache-stampede case, but balance updates need DB-level atomicity, not just an app-level mutex.
4. Idempotency keys on all money-moving endpoints.
5. Then transaction history, rate limiting, structured logging.

Want to start with the auth layer, or the lock race condition fix first?