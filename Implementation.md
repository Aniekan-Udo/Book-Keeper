# AI Bookkeeper v1 — Implementation Plan

Build a FastAPI backend for Nigerian small business bookkeeping: sales tracking, expense tracking, and P&L reports. Adapted from the existing payment-system auth layer.

## Decisions Locked In

| Decision | Choice |
|---|---|
| Target market | Nigerian small businesses (₦, local context) |
| Interface | API backend + simple web dashboard |
| AI provider | Provider-agnostic abstraction (swap later) |
| v1 scope | Core only — sales, expenses, basic P&L reports |

---

## Proposed Changes

### Auth Layer (Reuse from payment-system)

#### [REUSE] [auth.py](file:///Users/kanny/Desktop/payment-system/authentication/auth.py)
No changes needed — JWT access/refresh tokens, password hashing, token rotation all carry over as-is.

#### [REUSE] [settings.py](file:///Users/kanny/Desktop/payment-system/authentication/settings.py)
No changes needed.

#### [REUSE] [login.py](file:///Users/kanny/Desktop/payment-system/endpoints/login.py), [logout.py](file:///Users/kanny/Desktop/payment-system/endpoints/logout.py), [refresh.py](file:///Users/kanny/Desktop/payment-system/endpoints/refresh.py)
Reuse as-is (already using `get_db_session`).

---

### Database Layer

#### [MODIFY] [db.py](file:///Users/kanny/Desktop/payment-system/db.py)

**Keep:** `User` (remove `balance`), `RefreshToken`, `StepUpVerification`, `get_db_session`, engine config.

**Remove:** `Transaction`, `TransactionStatus`, `IdempotencyRecord` — payment-specific.

**Add new models:**

```python
class Organization(Model):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    industry: Mapped[str] = mapped_column(String(50), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NGN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=...)

class Membership(Model):
    __tablename__ = "memberships"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    org_id: Mapped[int] = mapped_column(Integer, ForeignKey("organizations.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="owner")
    # roles: "owner", "staff", "viewer"

class Category(Model):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    org_id: Mapped[int] = mapped_column(Integer, ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    type: Mapped[str] = mapped_column(String(10), nullable=False)  # "sale" or "expense"
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id"), nullable=True)

class Sale(Model):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    org_id: Mapped[int] = mapped_column(Integer, ForeignKey("organizations.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(DECIMAL(18, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id"), nullable=True)
    customer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "cash", "transfer", "pos"
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=...)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # soft delete

class Expense(Model):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    org_id: Mapped[int] = mapped_column(Integer, ForeignKey("organizations.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(DECIMAL(18, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id"), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=...)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
```

---

### Endpoints

#### [NEW] `endpoints/register.py`
User registration + automatic organization creation:
- `POST /register` — creates user, creates their organization, creates "owner" membership
- Request body: `email`, `password`, `firstname`, `lastname`, `business_name`, `industry` (optional)
- Returns: access token + refresh token (auto-login after registration)

#### [NEW] `endpoints/sales.py`
Full CRUD for sales, all scoped to the user's organization:
- `POST /sales` — record a sale
- `GET /sales` — list sales (filters: `date_from`, `date_to`, `category_id`, `min_amount`, `max_amount`, pagination)
- `GET /sales/{id}` — get single sale
- `PUT /sales/{id}` — update a sale
- `DELETE /sales/{id}` — soft-delete

#### [NEW] `endpoints/expenses.py`
Same pattern as sales:
- `POST /expenses` — record an expense
- `GET /expenses` — list with filters
- `GET /expenses/{id}` — get single
- `PUT /expenses/{id}` — update
- `DELETE /expenses/{id}` — soft-delete

#### [NEW] `endpoints/categories.py`
Manage custom categories per organization:
- `POST /categories` — create a category
- `GET /categories` — list categories (filter by type: sale/expense)
- `PUT /categories/{id}` — rename
- `DELETE /categories/{id}` — delete (only if no entries reference it)

Default categories seeded on org creation:
- **Sale types:** General Sales, Service Income, Other Income
- **Expense types:** Cost of Goods, Rent, Utilities, Transport, Salaries, Supplies, Other

#### [NEW] `endpoints/reports.py`
Read-only report endpoints:
- `GET /reports/pnl?from=2026-01-01&to=2026-07-31` — Profit & Loss statement
  - Returns: total sales, total expenses, gross profit, net profit/loss, breakdown by category
- `GET /reports/summary` — Dashboard overview for current period (today / this week / this month)
  - Returns: sales count & total, expenses count & total, net position, top categories
- `GET /reports/trends?period=monthly&months=6` — Month-over-month trends

#### [REMOVE] `endpoints/transfer.py`, `endpoints/check_balance.py`
Payment-specific — not needed.

#### [KEEP] `endpoints/verification.py`
Keep for later use (gating destructive actions like data export or account deletion). Not wired up in v1.

---

### Application Setup

#### [MODIFY] [main.py](file:///Users/kanny/Desktop/payment-system/main.py)
- Remove payment-specific routes (`transfer`, `check_balance`, `generate_report`)
- Remove Redis cache config (not needed for v1 — reports query the DB directly)
- Add new routers: `register`, `sales`, `expenses`, `categories`, `reports`
- Keep `get_current_user` dependency — add org context:

```python
async def get_current_user_with_org(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db_session),
):
    user_id = verify_token(token)
    result = await db.execute(
        select(User, Membership, Organization)
        .join(Membership, Membership.user_id == User.id)
        .join(Organization, Organization.id == Membership.org_id)
        .where(User.id == int(user_id))
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {"user": row.User, "membership": row.Membership, "org": row.Organization}
```

#### [NEW] `ai/__init__.py`, `ai/base.py`
Provider-agnostic AI abstraction — just the interface for now, no implementation in v1:

```python
# ai/base.py
from abc import ABC, abstractmethod

class BookkeeperAI(ABC):
    @abstractmethod
    async def parse_entry(self, text: str, categories: list[str]) -> dict:
        """Parse natural language into a structured sale/expense entry."""
        ...

    @abstractmethod
    async def ask(self, question: str, context: dict) -> str:
        """Answer a question about the business's finances."""
        ...
```

This gets implemented in v2 with whichever provider you choose.

---

### Migrations

#### [NEW] Alembic migration
After modifying `db.py`, generate a new migration:
```bash
alembic revision --autogenerate -m "bookkeeper_v1_models"
alembic upgrade head
```

> [!WARNING]
> The existing payment tables (`transactions`, `idempotency_records`) will be dropped by autogenerate. If you want to keep the payment-system database intact, create a **separate database** for the bookkeeper instead of migrating in-place. I'd recommend this approach — keep the payment-system as a reference.

---

## File Summary

| Action | File | Purpose |
|---|---|---|
| ✅ Keep | `authentication/auth.py` | JWT + password auth |
| ✅ Keep | `authentication/settings.py` | Config |
| ✅ Keep | `endpoints/login.py` | Login |
| ✅ Keep | `endpoints/logout.py` | Logout |
| ✅ Keep | `endpoints/refresh.py` | Token refresh |
| ✅ Keep | `endpoints/verification.py` | Step-up (future use) |
| ✏️ Modify | `db.py` | Remove payment models, add bookkeeper models |
| ✏️ Modify | `main.py` | Rewire routes, add org-aware user dependency |
| 🆕 New | `endpoints/register.py` | User + org registration |
| 🆕 New | `endpoints/sales.py` | Sales CRUD |
| 🆕 New | `endpoints/expenses.py` | Expenses CRUD |
| 🆕 New | `endpoints/categories.py` | Category management |
| 🆕 New | `endpoints/reports.py` | P&L, summary, trends |
| 🆕 New | `ai/base.py` | AI provider abstraction (stub) |
| ❌ Remove | `endpoints/transfer.py` | Payment-specific |
| ❌ Remove | `endpoints/check_balance.py` | Payment-specific |

---

## Verification Plan

### Automated Tests
```bash
# Run existing tests to ensure auth still works
pytest test/ -v

# Test new endpoints after implementation
pytest test/test_sales.py test/test_expenses.py test/test_reports.py -v
```

### Manual Verification
1. Register a new user → verify org + membership created
2. Login → record 5 sales, 3 expenses with different categories
3. Pull P&L report → verify totals match manual calculation
4. Test soft-delete → verify deleted entries excluded from reports
5. Test org scoping → verify user A can't see user B's data
