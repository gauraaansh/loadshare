"""
ARIA — LLM Client
==================
Thin wrapper around AsyncOpenAI pointing at vLLM (OpenAI-compatible API).

Usage:
    from llm import call_llm
    text = await call_llm(prompt, max_tokens=200)

Design:
  - One client instance, reused across all calls (connection pool inside openai SDK).
  - model is read from VLLM_MODEL env var (same as vLLM --model flag).
  - api_key is "EMPTY" — vLLM does not require auth, openai SDK requires a non-empty string.
  - Timeout: 30s per call. Agents should catch asyncio.TimeoutError and degrade gracefully.
  - LLM is ONLY called for natural-language synthesis. Never for computation/decisions.
"""

import os

import structlog
from openai import AsyncOpenAI

log = structlog.get_logger()

_VLLM_HOST   = os.getenv("VLLM_HOST",  "http://vllm:8000")
_VLLM_MODEL  = os.getenv("VLLM_MODEL", "Qwen/Qwen2.5-32B-Instruct-GPTQ-INT4")

# Single shared client — AsyncOpenAI uses aiohttp internally, safe to share.
_client = AsyncOpenAI(
    base_url=f"{_VLLM_HOST}/v1",
    api_key="EMPTY",
    timeout=30.0,
)


async def call_llm(
    prompt:      str,
    max_tokens:  int = 200,
    temperature: float = 0.2,
    system:      str | None = None,
) -> str:
    """
    Call the vLLM inference server and return the text response.

    Args:
        prompt:      The user prompt. Keep it short — context window is shared.
        max_tokens:  Hard cap on output tokens. Agents use 100-200 max.
        temperature: Low (0.1-0.2) for operational summaries, never 0 (boring).
        system:      Optional system message (role / instructions for the model).

    Returns:
        Stripped response string, or empty string on error (agent must handle).
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = await _client.chat.completions.create(
            model=_VLLM_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:
        log.warning("llm_call_failed", error=str(exc), prompt_len=len(prompt))
        return ""
