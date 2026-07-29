import asyncio
from llm import GroqModel, State, sys_msg
from tools.conversation import create_conversation, resolve_clarification
from db import get_db

async def test():
    state = State(
        user_message="I sold 20 bags of rice. James paid 500k for 15 trucks of cement. I borrowed 50k from sarah to pay for light.",
        sys_msg=sys_msg,
    )
    model = GroqModel(state)

    async with get_db() as db:
        transactions, clarifications = await create_conversation(db, model)
        print("Clarifications:", clarifications)

        if clarifications:
            txn_id = clarifications[0]["transaction_id"]
            resolved = await resolve_clarification(db, txn_id, amount=15000)
            print(f"Resolved: amount={resolved.amount} status={resolved.status}")

asyncio.run(test())