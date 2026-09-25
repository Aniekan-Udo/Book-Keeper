"""Public, read-only statement page behind an unguessable link (/s/<token>).

Shown to the trader's customer, so: escape everything the trader typed, keep it out of
search engines and caches, and never show anything but this one person's dealings.
There is deliberately no way to pay here — the app doesn't touch money.
"""
from html import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from config import BUSINESS_TZ
from db import User, get_db_session
from tools.formatting import format_naira
from tools.statements import Statement, StatementError, build_statement, resolve_link
from utils import limiter

router = APIRouter()

PRIVATE_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}

STYLE = """
:root {
  --bg: #f6f5f1; --card: #ffffff; --text: #1b1d22; --muted: #6b6f78; --line: #e6e3dc;
  --owe: #a4400d; --settled: #1d7a4a; --accent: #b37d18;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #0f1116; --card: #171a21; --text: #e8eaf0; --muted: #9aa0ab; --line: #272b35;
          --owe: #f0a15e; --settled: #5fd08f; --accent: #e8a325; }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 720px; margin: 0 auto; padding: 24px 16px 48px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 20px; }
.eyebrow { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
h1 { font-size: 22px; margin: 4px 0 2px; }
.sub { color: var(--muted); font-size: 14px; }
.balance { margin: 20px 0; padding: 16px; border-radius: 10px; background: var(--bg); }
.balance .amount { font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; }
.balance.owes .amount { color: var(--owe); }
.balance.settled .amount { color: var(--settled); }
.row { display: grid; grid-template-columns: 1fr auto; gap: 2px 12px; padding: 12px 0; border-top: 1px solid var(--line); }
.row .desc { font-weight: 500; overflow-wrap: anywhere; }
.row .amt, .row .bal { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.row .date, .row .bal { color: var(--muted); font-size: 13px; }
.row.head { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; border-top: none; padding-top: 0; }
.note { color: var(--muted); font-size: 13px; margin-top: 16px; }
footer { color: var(--muted); font-size: 12px; text-align: center; margin-top: 20px; }
@media print {
  body { background: #fff; color: #000; }
  .card { border: none; padding: 0; }
  .balance { background: #f2f2f2; }
}
"""


def _row(date_text: str, desc: str, amount: str, balance: str, css: str = "row") -> str:
    return (f'<div class="{css}"><span class="desc">{desc}</span><span class="amt">{amount}</span>'
            f'<span class="date">{date_text}</span><span class="bal">{balance}</span></div>')


def render_statement(st: Statement) -> str:
    e = escape
    owes = st.closing_balance != 0
    period = ""
    if st.start or st.end:
        period = " · " + " – ".join(d.strftime("%b %d, %Y") if d else "…" for d in (st.start, st.end))
    generated = st.generated_at.astimezone(BUSINESS_TZ).strftime("%b %d, %Y, %I:%M %p")

    rows = [_row("Date", "Details", "Amount", "Balance", css="row head")]
    if st.opening_balance:
        rows.append(_row("", "Brought forward", "", e(format_naira(st.opening_balance))))
    for line in st.lines:
        sign = "+" if line.amount > 0 else "−"
        when = line.date.strftime("%b %d, %Y")
        if line.due:
            when += f" · due {line.due.strftime('%b %d')}"
        rows.append(_row(e(when), e(line.description),
                         f"{sign}{e(format_naira(abs(line.amount)))}", e(format_naira(line.balance))))
    if not st.lines:
        rows.append('<p class="note">No entries in this period.</p>')

    pending = ""
    if st.pending_amounts:
        pending = (f'<p class="note">{st.pending_amounts} more entr{"y" if st.pending_amounts == 1 else "ies"} '
                   f"without an amount yet {'is' if st.pending_amounts == 1 else 'are'} not included.</p>")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Statement — {e(st.party)}</title>
<style>{STYLE}</style>
</head>
<body>
<main>
  <div class="card">
    <div class="eyebrow">Statement of account</div>
    <h1>{e(st.party)}</h1>
    <div class="sub">from {e(st.business)}{e(period)}</div>
    <div class="balance {'owes' if owes else 'settled'}">
      <div class="sub">Balance</div>
      <div class="amount">{e(format_naira(abs(st.closing_balance)))}</div>
      <div>{e(st.balance_sentence())}</div>
    </div>
    {''.join(rows)}
    {pending}
    <p class="note">For questions about this statement, please contact {e(st.business)} directly.</p>
  </div>
  <footer>Up to date as of {e(generated)} · Prepared with Book-Keeper</footer>
</main>
</body>
</html>"""


NOT_FOUND_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Statement unavailable</title><style>""" + STYLE + """</style></head>
<body><main><div class="card"><h1>This statement link isn't available</h1>
<p class="sub" style="margin-top:8px">It may have expired or been withdrawn. Please ask the business for a new link.</p>
</div></main></body></html>"""


@router.get("/s/{token}", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("30/minute")
async def statement_page(request: Request, token: str, db: AsyncSession = Depends(get_db_session)):
    link = await resolve_link(db, token)
    if link is None:
        return HTMLResponse(NOT_FOUND_PAGE, status_code=404, headers=PRIVATE_HEADERS)
    user = await db.get(User, link.user_id)
    try:
        statement = await build_statement(db, user, link.party)
    except StatementError:
        return HTMLResponse(NOT_FOUND_PAGE, status_code=404, headers=PRIVATE_HEADERS)
    return HTMLResponse(render_statement(statement), headers=PRIVATE_HEADERS)
