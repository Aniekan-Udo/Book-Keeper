"""The web app's pages and read-only views: landing page, app shell, overview, chase,
people, one person, history, and the demo login (off unless DEMO_ACCOUNTS is set)."""
from datetime import date

import pytest

from db import ConversationTurn, User
from endpoints.demo import DAY30
from authentication.auth import hash_password
from tools.ledger import TransactionInput, persist_transactions

PASSWORD = "correct-horse-battery-staple"


async def logged_in(new_browser, test_user):
    browser = await new_browser()
    res = await browser.post("/login", json={"email": test_user.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return browser


async def books(db_session, user):
    await persist_transactions(db_session, [
        TransactionInput(type="sale", party="Emeka", item="beans", amount=60000, amount_paid=0, direction="in",
                         date=date(2026, 9, 2), due_date=date(2026, 9, 7)),
        TransactionInput(type="sale", party="Bola", item="rice", amount=90000, direction="in", date=date(2026, 9, 5)),
        TransactionInput(type="expense", party="Alhaji Musa", item="palm oil", amount=120000, amount_paid=70000,
                         direction="out", date=date(2026, 9, 16)),
    ], user.id)


async def test_landing_and_app_pages_are_served(new_browser):
    browser = await new_browser()
    landing = await browser.get("/")
    assert landing.status_code == 200 and "books" in landing.text and "who owes you" in landing.text
    app = await browser.get("/app")
    assert app.status_code == 200 and 'src="/static/assets/app.js"' in app.text
    for asset in ("base.css", "landing.css", "app.css", "app.js"):
        assert (await browser.get(f"/static/assets/{asset}")).status_code == 200, asset


async def test_views_need_a_login(new_browser):
    browser = await new_browser()
    for path in ("/api/v1/overview", "/api/v1/debts", "/api/v1/chase", "/api/v1/people", "/api/v1/people/Emeka",
                 "/api/v1/history"):
        assert (await browser.get(path)).status_code == 401, path


async def test_overview(new_browser, test_user, db_session):
    await books(db_session, test_user)
    browser = await logged_in(new_browser, test_user)
    d = (await browser.get("/api/v1/overview?period=all")).json()

    assert (d["sales"], d["expenses"], d["profit"]) == (150000, 120000, 30000)
    assert d["owed_to_you"] == {"total": 60000, "people": [{"name": "Emeka", "amount": 60000}]}
    assert d["you_owe"] == {"total": 50000, "people": [{"name": "Alhaji Musa", "amount": 50000}]}
    assert [r["party"] for r in d["recent"]] == ["Alhaji Musa", "Bola", "Emeka"]
    month = (await browser.get("/api/v1/overview?period=month")).json()
    assert month["period"] != "All time"


async def test_chase_people_and_person_without_graph_memory(new_browser, test_user, db_session):
    """Memory is off in tests: the views still work from the ledger alone."""
    await books(db_session, test_user)
    browser = await logged_in(new_browser, test_user)

    chase = (await browser.get("/api/v1/chase")).json()
    assert [c["name"] for c in chase["chase"]] == ["Emeka"] and chase["ranked_by"].startswith("days overdue")

    people = (await browser.get("/api/v1/people")).json()
    assert people["memory"] == "off"
    by_name = {p["name"]: p for p in people["people"]}
    assert by_name["Emeka"]["owes_you"] == 60000 and by_name["Alhaji Musa"]["you_owe"] == 50000
    assert by_name["Bola"]["promises"] is None

    emeka = (await browser.get("/api/v1/people/emeka")).json()
    assert emeka["name"] == "Emeka" and emeka["entries"][0]["due_date"] == "2026-09-07"
    assert "error" in emeka["memory"]  # memory off: said plainly, not an error page
    assert (await browser.get("/api/v1/people/Nobody")).status_code == 404


async def test_views_only_show_your_own_books(new_browser, test_user, other_user, db_session):
    await books(db_session, other_user)
    browser = await logged_in(new_browser, test_user)
    assert (await browser.get("/api/v1/people")).json()["people"] == []
    assert (await browser.get("/api/v1/overview?period=all")).json()["sales"] == 0
    assert (await browser.get("/api/v1/people/Emeka")).status_code == 404


async def test_history_returns_the_conversation_oldest_first(new_browser, test_user, db_session):
    db_session.add_all([ConversationTurn(user_id=test_user.id, role="user", content="I sold rice", channel="web"),
                        ConversationTurn(user_id=test_user.id, role="assistant", content="Recorded.", channel="web")])
    await db_session.commit()
    browser = await logged_in(new_browser, test_user)
    turns = (await browser.get("/api/v1/history")).json()["turns"]
    assert [(t["role"], t["content"]) for t in turns] == [("user", "I sold rice"), ("assistant", "Recorded.")]


async def test_demo_login_is_off_by_default(new_browser, monkeypatch):
    monkeypatch.delenv("DEMO_ACCOUNTS", raising=False)
    browser = await new_browser()
    assert (await browser.get("/demo/info")).json() == {"enabled": False, "accounts": []}
    assert (await browser.post("/demo/login", json={"account": "day30"})).status_code == 404


async def test_demo_login_when_switched_on(new_browser, db_session, monkeypatch):
    monkeypatch.setenv("DEMO_ACCOUNTS", "true")
    browser = await new_browser()
    assert (await browser.get("/demo/info")).json()["enabled"] is False  # switched on, but not seeded here

    db_session.add(User(firstname="Ngozi", lastname="Eze", email=DAY30[0], password=hash_password("x" * 12),
                        business_name=DAY30[1]))
    await db_session.commit()
    info = (await browser.get("/demo/info")).json()
    assert info["enabled"] is True and [a["id"] for a in info["accounts"]] == ["day30"]

    assert (await browser.post("/demo/login", json={"account": "day30"})).status_code == 200
    assert (await browser.get("/api/v1/me")).json()["business_name"] == DAY30[1]
    assert (await browser.post("/demo/login", json={"account": "day1"})).status_code == 404  # not seeded
    assert (await browser.post("/demo/login", json={"account": "admin"})).status_code == 422


async def test_overview_by_item_and_by_period(new_browser, test_user, db_session):
    from config import business_today
    today = business_today()
    await persist_transactions(db_session, [
        TransactionInput(type="sale", party="Bola", item="rice", quantity=2, unit="bags", amount=90000,
                         direction="in", date=today),
        TransactionInput(type="sale", party="Emeka", item="Rice", quantity=1, unit="bag", amount=45000,
                         amount_paid=0, direction="in", date=today),
        TransactionInput(type="sale", party="Tola", item="beans", amount=30000, direction="in", date=date(2020, 1, 1)),
        TransactionInput(type="expense", item="fuel", amount=5000, direction="out", date=today),
    ], test_user.id)
    browser = await logged_in(new_browser, test_user)

    day = (await browser.get("/api/v1/overview?period=today")).json()
    assert (day["sales"], day["expenses"], day["sales_on_credit"]) == (135000, 5000, 45000)
    assert day["sales_by_item"] == [{"item": "rice", "amount": 135000, "entries": 2, "quantity": 3, "unit": "bags"}]
    assert day["expenses_by_item"][0]["item"] == "fuel"
    week = (await browser.get("/api/v1/overview?period=week")).json()
    assert week["sales"] == 135000 and week["period"].startswith("This week")
    assert (await browser.get("/api/v1/overview?period=all")).json()["sales"] == 165000


async def test_debts_say_who_owes_what_for_what_and_when(new_browser, test_user, db_session):
    await books(db_session, test_user)
    await persist_transactions(db_session, [
        TransactionInput(type="debt_repayment", party="Emeka", amount=20000, direction="in", date=date(2026, 9, 6)),
    ], test_user.id)
    browser = await logged_in(new_browser, test_user)
    d = (await browser.get("/api/v1/debts")).json()

    [emeka] = d["owed_to_you"]["people"]
    assert emeka["name"] == "Emeka" and emeka["balance"] == 40000 and emeka["overdue"] is True
    [entry] = emeka["entries"]
    assert (entry["item"], entry["amount"], entry["paid"], entry["balance"], entry["due_date"]) == \
        ("beans", 60000, 20000, 40000, "2026-09-07")
    assert entry["days_overdue"] > 0 and d["owed_to_you"]["overdue"] == 40000
    [musa] = d["you_owe"]["people"]
    assert (musa["name"], musa["balance"], musa["entries"][0]["item"]) == ("Alhaji Musa", 50000, "palm oil")


async def test_security_headers_on_pages_and_api(new_browser):
    browser = await new_browser()
    for path in ("/", "/app", "/health", "/api/v1/overview"):
        h = (await browser.get(path)).headers
        assert "script-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"], path
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY", path
        assert "camera=()" in h["permissions-policy"], path
        assert "'unsafe-inline'" not in h["content-security-policy"] and "https:" not in h["content-security-policy"], path
        assert h["cross-origin-resource-policy"] == "same-origin", path
    assert (await browser.get("/api/v1/overview")).headers["cache-control"] == "no-store"
    assert (await browser.get("/static/assets/fonts/inter-latin.woff2")).status_code == 200


async def test_frontend_has_no_inline_scripts_or_handlers():
    """The CSP allows scripts from this server only: inline ones would silently stop working."""
    import re
    from pathlib import Path
    for page in ("index.html", "app.html"):
        html = (Path(__file__).parent.parent / "frontend" / page).read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), page
        assert not re.search(r"\son[a-z]+=\"", html), page
        assert ' style="' not in html and "fonts.googleapis" not in html, page
