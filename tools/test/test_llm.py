"""Model selection: `provider:model` strings, including OpenRouter (OpenAI-compatible)."""
from langchain_openai import ChatOpenAI

import llm


def test_openrouter_uses_the_openai_client_at_openrouters_address(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    model = llm._build("openrouter:openai/gpt-oss-120b", timeout=30)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "openai/gpt-oss-120b"
    assert model.openai_api_base == "https://openrouter.ai/api/v1"
    assert model.openai_api_key.get_secret_value() == "sk-or-test"
    assert model.temperature == 0
    assert model.extra_body == {"provider": {"order": ["groq", "cerebras"], "allow_fallbacks": True},
                                "reasoning": {"effort": "medium"}}


def test_the_openrouter_address_can_be_overridden(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.example.com/v1")
    assert llm._build("openrouter:openai/gpt-oss-120b", timeout=30).openai_api_base == "https://proxy.example.com/v1"


def test_openrouter_connections_are_not_reused_after_sitting_idle(monkeypatch):
    """A pooled connection that died while idle made the next message wait out the timeout."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    model = llm._build("openrouter:openai/gpt-oss-120b", timeout=20)
    pool = model.http_async_client._transport._pool
    assert pool._keepalive_expiry == llm.KEEPALIVE_SECONDS <= 30
    assert model.http_async_client.timeout.read == 20
