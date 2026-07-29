import asyncio
from sqlalchemy import text
from db import get_db
from tools.router import handle_message
from tools.clean_version import build_tools
from llm import router_sys_msg, llm


async def reset_db():
    async with get_db() as db:
        await db.execute(text("TRUNCATE TABLE business_transactions RESTART IDENTITY"))
        await db.commit()


def print_full_result(result: dict):
    for msg in result["messages"]:
        msg_type = type(msg).__name__
        tool_calls = getattr(msg, "tool_calls", None)
        content = msg.content
        print(f"--- {msg_type} ---")
        if tool_calls:
            print(f"  tool_calls: {tool_calls}")
        print(f"  content: {content!r}")
    print()


async def run_message(user_message: str, db):
    tools = build_tools(db)
    print(f"Tools bound: {[t.name for t in tools]}")
    sys_msg = router_sys_msg()
    result = await handle_message(
        user_message=user_message,
        db=db,
        system_prompt=sys_msg,
        model=llm,
        tools=tools,
    )
    return result


async def test():
    await reset_db()

    async with get_db() as db:
        print("=== Test 1: transactions (sale, expense, loan) ===")
        result1 = await run_message(
            "I sold 20 bags of rice for 15k. James paid 500k for 15 trucks of cement. "
            "I paid 20k for fuel. I borrowed 50k from sarah to pay for light.",
            db,
        )
        print_full_result(result1)

        print("=== Test 2: PnL request ===")
        result2 = await run_message("What's my PnL for this week?", db)
        print_full_result(result2)

        print("=== Test 3: expenses request ===")
        result3 = await run_message("What are my expenses this month?", db)
        print_full_result(result3)


asyncio.run(test())