import asyncio
from datetime import date
from db import get_db
from config import GenerateDate
from tools.expenses import get_expenses
from tools.generate_PnL import generate_pnl

async def test():
    async with get_db() as db:
        # no date filter — pull everything currently in the table
        dates = GenerateDate()

        pnl = await generate_pnl(db, dates)
        print("PnL:", pnl)

        expenses = await get_expenses(db, dates)
        print("\nExpenses:")
        for e in expenses:
            print(f"  {e['party']} | {e['item']} | {e['amount']} | {e['date']}")

asyncio.run(test())