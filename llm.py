from pydantic import BaseModel
from abc import ABC, abstractmethod
from langchain_groq import ChatGroq
import os
from config import ExtractionResult
from langchain_core.messages import SystemMessage, HumanMessage
from datetime import date

from dotenv import load_dotenv
load_dotenv()


class State(BaseModel):
    user_message: str
    sys_msg: str


from datetime import date

def router_sys_msg() -> str:
    today = date.today().isoformat()
    return f"""You are a bookkeeping assistant. Today's date is {today}.

You have three tools:
1. extract_transactions_tool — use this when the user is describing something that happened: a sale, an expense, a loan, a repayment.
2. generate_report_tool — use this when the user is asking for PnL, profit, net income, expenses, or what customers owe them (receivables) over some period.
3. query_transactions_tool — use this when the user asks about a specific past transaction: "what did I spend X on", "who did I pay Y to", "remind me about that transaction" — as opposed to a summary total.

Call exactly one tool that matches the user's intent.

When you need to use a tool, call it directly without announcing the tool name or explaining that you're using a tool.
Never mention internal tool names in your response to the user.

## Transaction types
- "sale": the business received (or will receive) money for goods/services it sold to a customer. direction="in".
- "expense": the business paid money out for goods/services it bought. direction="out".
- "loan_taken": the business borrowed money from someone. direction="in". party is the lender.
- "loan_given": the business lent money to someone. direction="out". party is the borrower.
- "debt_repayment": the business paid back money it previously owed, or received repayment of money it previously lent.

## Disambiguating who paid whom
[... paste your full disambiguation examples here — "I sold 20 bags of rice", "James paid me 500k for cement", etc. ...]

## Amount rule — critical
If the message does not state a specific numeric amount for a transaction, set amount = null and status = "needs_amount". Never guess or default an amount.

## Amount normalization
Convert shorthand to raw numbers: "500k" → 500000, "50k" → 50000.

## No fabrication
Only fill item/unit/party/note when the message actually states them.

## Bundled transactions
If a transaction states what borrowed or spent money was used for (e.g. "I borrowed 50k to buy fuel"), extract it as TWO separate transactions: the loan_taken itself, and a separate expense for what the money was used for.

## Partial payments
If a sale states only part of the agreed amount was received, set amount = the full agreed amount and amount_paid = what was actually received. If not mentioned, leave amount_paid unset.

## For generate_report_tool
report_type = "pnl" for profit/net income questions, "expenses" for spending questions, "receivables" for "what's owed to me" / "who hasn't paid". Convert relative time language ("this week", "last month", "today") into actual start_date/end_date using today's real date above. Leave both null for "all time".
"""

from langchain_groq import ChatGroq
import os

# llm = ChatGroq(
#      model=os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"),
#      api_key=os.getenv("CHAT_GROQ_API_KEY"),
#      max_retries=2,
#      timeout=15,
#  )

# from langchain_google_genai import ChatGoogleGenerativeAI
# llm = ChatGoogleGenerativeAI(
#        model="gemini-2.5-flash",
#        api_key=os.getenv("GEMINI_API_KEY"),
#        max_retries=2,
#        timeout=15,
#  )

from langchain_nvidia_ai_endpoints import ChatNVIDIA

llm = ChatNVIDIA(
    model="meta/llama-3.3-70b-instruct",
    api_key=os.getenv("NVIDIA_API_KEY"),
    timeout=60,
)




class Models(ABC):
    def __init__(self, state: State, tools: list, tool_name: str):
        self.state = state
        self.tools = tools
        self.tool_name = tool_name
        self.llm = None
        self.bound_llm = None
        self.response = None

    @abstractmethod
    async def get_model(self):
        ...

    @abstractmethod
    async def bind(self):
        """Bind self.tools to self.llm and store the result in self.bound_llm."""
        ...

    @abstractmethod
    async def model_response(self):
        ...

    @abstractmethod
    async def tool_call(self) -> dict:
        """Must return {'name': str, 'args': dict} regardless of provider."""
        ...


class GroqModel(Models):
    async def get_model(self):
        self.llm = ChatGroq(
            model=os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"),
            api_key=os.getenv("CHAT_GROQ_API_KEY"),
        )
        return self.llm

    async def bind(self):
        llm = self.llm or await self.get_model()
        self.bound_llm = llm.bind_tools(self.tools, tool_choice=self.tool_name)
        return self.bound_llm

    async def model_response(self):
        bound_llm = self.bound_llm or await self.bind()
        self.response = await bound_llm.ainvoke([
            SystemMessage(content=self.state.sys_msg),
            HumanMessage(content=self.state.user_message),
        ])
        return self.response

    async def tool_call(self) -> dict:
        response = await self.model_response()
        tool_block = next(c for c in response.tool_calls if c["name"] == self.tool_name)
        return {"name": tool_block["name"], "args": tool_block["args"]}


class AnthropicModel(Models):
    async def get_model(self):
        self.llm = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        return self.llm

    async def bind(self):
        # Anthropic's raw SDK has no separate .bind_tools step — tools are
        # passed per-call to .messages.create(). "Binding" here just means
        # confirming the client exists and tools are ready to attach.
        self.llm = self.llm or await self.get_model()
        self.bound_llm = self.llm
        return self.bound_llm

    async def model_response(self):
        bound_llm = self.bound_llm or await self.bind()
        self.response = await bound_llm.messages.create(
            model=os.getenv("ANTHROPIC_MODEL"),
            max_tokens=1024,
            tools=self.tools,
            tool_choice={"type": "tool", "name": self.tool_name},
            system=self.state.sys_msg,
            messages=[{"role": "user", "content": self.state.user_message}]
        )
        return self.response

    async def tool_call(self) -> dict:
        response = await self.model_response()
        tool_block = next(b for b in response.content if b.type == "tool_use")
        return {"name": tool_block.name, "args": tool_block.input}