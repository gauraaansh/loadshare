"""
ARIA — Docs Chat
=================
RAG chatbot over ARIA_DOCS.md using local infrastructure:
  - Embedding:  Ollama nomic-embed-text (768-dim, same as supervisor RAG)
  - Vector DB:  pgvector in existing Postgres (docs_chunks table)
  - Generation: vLLM Qwen2.5 via OpenAI-compatible streaming API

Endpoints:
  POST /docs-chat/chat    — embed query → vector search → stream vLLM answer
  POST /docs-chat/ingest  — parse ARIA_DOCS.md → embed → store in pgvector
"""

import os
import re

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.security.api_key import APIKeyHeader
from openai import AsyncOpenAI
from pydantic import BaseModel

import embedding_client
from config import MCP_API_KEY, VLLM_HOST
from db import get_pool

log = structlog.get_logger()

DOCS_PATH  = os.getenv("ARIA_DOCS_PATH", "/docs/ARIA_DOCS.md")
VLLM_MODEL = os.getenv("VLLM_MODEL",     "Qwen/Qwen2.5-32B-Instruct-GPTQ-INT4")

_llm = AsyncOpenAI(base_url=f"{VLLM_HOST}/v1", api_key="EMPTY", timeout=60.0)

router = APIRouter(prefix="/docs-chat", tags=["docs-chat"])

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def require_api_key(key: str = Depends(_api_key_header)):
    if key != MCP_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return key


# ── Chunking ──────────────────────────────────────────────────────────────────

def _parse_chunks(text: str) -> list[dict]:
    pattern = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    chunks  = []
    for i, match in enumerate(matches):
        level   = len(match.group(1)) - 1
        title   = match.group(2).strip()
        start   = match.end()
        end     = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if not content:
            continue
        if len(content) > 6000:
            content = content[:6000] + "\n\n[truncated]"
        chunks.append({"section": title, "level": level, "content": content})
    return chunks


# ── Prompt ────────────────────────────────────────────────────────────────────

def _build_prompt(query: str, chunks: list[dict]) -> str:
    context = "\n\n---\n\n".join(
        f"### {c['section']}\n{c['content'].strip()}" for c in chunks
    )
    return (
        "You are an expert on the ARIA system (Autonomous Rider Intelligence & "
        "Analytics System), a portfolio project built for an AI Engineer role at "
        "Loadshare Networks.\n\n"
        "Answer the question using the documentation excerpts below. Be technical "
        "when the question calls for it, concise, and honest — if the answer is "
        "not in the documentation, say so clearly.\n\n"
        f"DOCUMENTATION:\n{context}\n\n"
        f"QUESTION: {query}\n\nANSWER:"
    )


# ── vLLM streaming generator ──────────────────────────────────────────────────

async def _stream_answer(prompt: str):
    try:
        stream = await _llm.chat.completions.create(
            model       = VLLM_MODEL,
            messages    = [{"role": "user", "content": prompt}],
            max_tokens  = 1024,
            temperature = 0.3,
            stream      = True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except Exception as exc:
        log.warning("docs_chat_llm_error", error=str(exc))
        yield f"\n[Error generating response: {exc}]"


# ── Endpoints ─────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str


@router.post("/chat")
async def docs_chat(body: ChatRequest, _=Depends(require_api_key)):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message required")
    if len(body.message) > 500:
        raise HTTPException(status_code=400, detail="message too long")

    # Embed query with Ollama nomic-embed-text
    embedding, _ = await embedding_client.get_embedding(body.message)
    if embedding is None:
        raise HTTPException(status_code=503, detail="Embedding service unavailable")

    vec_str = embedding_client.vec_to_pgvector_str(embedding)

    # pgvector cosine similarity search
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT section, content,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM   docs_chunks
            ORDER  BY embedding <=> $1::vector
            LIMIT  5
            """,
            vec_str,
        )

    if not rows:
        raise HTTPException(
            status_code=503,
            detail="Docs not ingested yet — call POST /docs-chat/ingest first.",
        )

    chunks = [{"section": r["section"], "content": r["content"]} for r in rows]
    prompt = _build_prompt(body.message, chunks)

    return StreamingResponse(_stream_answer(prompt), media_type="text/plain; charset=utf-8")


@router.post("/ingest")
async def docs_ingest(_=Depends(require_api_key)):
    """
    Parse ARIA_DOCS.md into chunks, embed each with Ollama nomic-embed-text,
    and store in the docs_chunks pgvector table. Safe to re-run — truncates first.
    """
    if not os.path.exists(DOCS_PATH):
        raise HTTPException(
            status_code=404,
            detail=f"Docs file not found at {DOCS_PATH}. Check ARIA_DOCS_PATH env var and volume mount.",
        )

    text   = open(DOCS_PATH, encoding="utf-8").read()
    chunks = _parse_chunks(text)
    log.info("docs_ingest_start", total_chunks=len(chunks))

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE docs_chunks RESTART IDENTITY")
        ok = 0
        for chunk in chunks:
            embed_text    = f"{chunk['section']}\n\n{chunk['content']}"
            vec, latency  = await embedding_client.get_embedding(embed_text)
            if vec is None:
                log.warning("docs_ingest_embed_failed", section=chunk["section"])
                continue
            vec_str = embedding_client.vec_to_pgvector_str(vec)
            await conn.execute(
                "INSERT INTO docs_chunks (section, level, content, embedding) "
                "VALUES ($1, $2, $3, $4::vector)",
                chunk["section"], chunk["level"], chunk["content"], vec_str,
            )
            ok += 1

    log.info("docs_ingest_done", inserted=ok, total=len(chunks))
    return {"status": "ok", "inserted": ok, "total": len(chunks)}
