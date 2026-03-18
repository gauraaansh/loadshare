"""
ARIA — Embedding Client
========================
Thin async wrapper around the Ollama /api/embeddings endpoint.

Used by the Supervisor Agent (Phase 2 RAG) and the Docs Chat RAG to embed
text for storage and retrieval.

Model: qwen3-embedding (4096-dim, MTEB #1 as of 2025).
Supports asymmetric retrieval via query instruction prefix:
  - Queries get an instruction prefix so the model produces a query-optimised vector.
  - Documents are embedded raw (no prefix) for passage-side representation.
  This directly improves informal query → formal documentation retrieval.

Design:
  - Shared httpx.AsyncClient with connection pooling.
  - 60s timeout — qwen3-embedding is larger than nomic-embed-text, give headroom.
  - On failure: returns (None, latency_ms). Caller writes embedding_status='failed'
    and continues without RAG. Supervisor never blocks on embedding errors.
  - Vector dimension: 4096 (qwen3-embedding default). Matches vector(4096) in schema.
  - Zero vector fallback: if Ollama fails, caller writes [0.0]*4096 with
    embedding_status='failed'. These rows are excluded from retrieval by
    WHERE embedding_status = 'ok' filter.
"""

import os
import time
from typing import Optional

import httpx
import structlog

log = structlog.get_logger()

_OLLAMA_HOST  = os.getenv("OLLAMA_HOST",            "http://ollama:11434")
_EMBED_MODEL  = os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding")
_VECTOR_DIM   = 4096
_ZERO_VECTOR  = [0.0] * _VECTOR_DIM

# Instruction prefix for query-side embedding (asymmetric retrieval).
# qwen3-embedding was trained with this pattern — omitting it on queries
# drops retrieval accuracy for informal → formal document matching.
_QUERY_INSTRUCTION = (
    "Instruct: Given a conversational user question, retrieve the most relevant "
    "technical documentation passage that answers it.\nQuery: "
)

_client = httpx.AsyncClient(
    base_url=_OLLAMA_HOST,
    timeout=60.0,
    limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
)


async def get_embedding(
    text: str,
    is_query: bool = False,
) -> tuple[Optional[list[float]], int]:
    """
    Embed text via Ollama qwen3-embedding.

    Args:
        text:     The text to embed (document chunk or user query).
        is_query: If True, prepends the asymmetric retrieval instruction prefix.
                  Set True for user queries; False (default) for documents/passages.

    Returns:
        (embedding, latency_ms) where embedding is list[float] of length 4096,
        or (None, latency_ms) on any failure.
    """
    prompt = (_QUERY_INSTRUCTION + text) if is_query else text
    t0 = time.monotonic()
    try:
        resp = await _client.post(
            "/api/embeddings",
            json={"model": _EMBED_MODEL, "prompt": prompt},
        )
        resp.raise_for_status()
        data      = resp.json()
        embedding = data.get("embedding")
        latency   = int((time.monotonic() - t0) * 1000)

        if not embedding or not isinstance(embedding, list):
            log.warning("embedding_empty_response", model=_EMBED_MODEL)
            return None, latency

        if len(embedding) != _VECTOR_DIM:
            log.warning(
                "embedding_dimension_mismatch",
                expected=_VECTOR_DIM,
                got=len(embedding),
            )
            return None, latency

        log.debug("embedding_ok", latency_ms=latency, text_len=len(text), is_query=is_query)
        return [float(v) for v in embedding], latency

    except httpx.HTTPStatusError as exc:
        latency = int((time.monotonic() - t0) * 1000)
        log.warning("embedding_http_error", status=exc.response.status_code, latency_ms=latency)
        return None, latency
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        log.warning("embedding_failed", error=str(exc), latency_ms=latency)
        return None, latency


def vec_to_pgvector_str(vec: list[float]) -> str:
    """
    Format a Python float list as a pgvector literal string.
    asyncpg does not natively understand vector type — pass as text,
    cast to vector in the SQL query ($1::vector).

    Example: [0.1, 0.2, 0.3] → '[0.1,0.2,0.3]'
    """
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


async def close() -> None:
    """Call on application shutdown."""
    await _client.aclose()
