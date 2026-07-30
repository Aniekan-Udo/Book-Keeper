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

import os
import logging
from itertools import cycle
from langchain_google_genai import ChatGoogleGenerativeAI

logger = logging.getLogger("bookkeeper")

GEMINI_KEYS = [
    k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()
]
if not GEMINI_KEYS:
    raise RuntimeError("No Gemini API keys configured in GEMINI_API_KEYS")

_key_cycle = cycle(GEMINI_KEYS)


def get_gemini_model() -> ChatGoogleGenerativeAI:
    key = next(_key_cycle)
    return ChatGoogleGenerativeAI(
        model=os.getenv("LLM_MODEL", "gemini-2.5-flash"),
        api_key=key,
    )


# async def call_with_key_rotation(agent_factory, tools, system_prompt, user_message):
#     last_error = None
#     for _ in range(len(GEMINI_KEYS)):
#         model = get_gemini_model()
#         try:
#             agent = agent_factory(model=model, tools=tools, system_prompt=system_prompt)
#             return await agent.ainvoke({"messages": [{"role": "user", "content": user_message}]})
#         except Exception as e:
#             if is_rate_limit_error(e):
#                 logger.warning("Gemini key rate-limited, rotating to next key")
#                 last_error = e
#                 continue
#             raise
#     raise last_error or RuntimeError("All Gemini keys exhausted")


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