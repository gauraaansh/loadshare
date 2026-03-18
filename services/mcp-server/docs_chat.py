"""
ARIA — Docs Chat
=================
RAG chatbot over ARIA_DOCS.md with two retrieval modes:

  Vector RAG   — embed query → pgvector cosine similarity → top-k chunks → stream answer
  PageIndex    — parse doc tree → LLM reasons over section titles → fetch full text → stream answer

Endpoints:
  POST /docs-chat/chat         — chat (mode: "vector" | "pageindex")
  POST /docs-chat/ingest       — build vector index (embed all chunks into pgvector)
  POST /docs-chat/build-index  — build PageIndex tree (save to /tmp/aria_docs_index.json)
"""

import json
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

DOCS_PATH   = os.getenv("ARIA_DOCS_PATH",  "/docs/ARIA_DOCS.md")
NAV_PATH    = os.getenv("ARIA_NAV_PATH",   "/docs/ARIA_NAV.md")
INDEX_PATH  = os.getenv("ARIA_INDEX_PATH", "/tmp/aria_docs_index.json")
VLLM_MODEL  = os.getenv("VLLM_MODEL",      "Qwen/Qwen2.5-32B-Instruct-GPTQ-INT4")

_llm = AsyncOpenAI(base_url=f"{VLLM_HOST}/v1", api_key="EMPTY", timeout=60.0)

router = APIRouter(prefix="/docs-chat", tags=["docs-chat"])

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def require_api_key(key: str = Depends(_api_key_header)):
    if key != MCP_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return key


# ── Chunking ──────────────────────────────────────────────────────────────────

# Real top-level chapters start with a digit (e.g. "1. System Overview") or "ARIA"
_CHAPTER_RE = re.compile(r"^(\d+\.|ARIA)")

def _parse_chunks(text: str) -> list[dict]:
    """
    Parse ARIA_DOCS.md into a 3-level hierarchy:
      level 0  (#)   — real chapters only (numbered or "ARIA …")
      level 1  (##)  — sections (content nodes)
      level 2  (###) — subsections (content nodes)
    Spurious # lines that are inline code/math are silently skipped.
    """
    pattern = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    chunks  = []
    for i, match in enumerate(matches):
        raw_level = len(match.group(1))
        level     = raw_level - 1        # # → 0, ## → 1, ### → 2
        title     = match.group(2).strip()

        # Skip # headers that are not real chapters (inline code / narrative lines)
        if level == 0 and not _CHAPTER_RE.match(title):
            continue

        start   = match.end()
        end     = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()

        if level == 0:
            # Chapter nodes are navigation-only; content is empty
            chunks.append({"section": title, "level": 0, "content": ""})
            continue

        if not content:
            continue
        if len(content) > 900:
            content = content[:900] + "\n\n[truncated]"
        chunks.append({"section": title, "level": level, "content": content})
    return chunks


# ── Prompt ────────────────────────────────────────────────────────────────────

def _build_prompt(query: str, chunks: list[dict]) -> str:
    # Cap each chunk at 1200 chars. With max 5 chunks: 5×1200=6000 chars ≈ 1500 tokens.
    # Plus ~300 token prompt overhead + 700 tokens output = 2500 total — within 4096 vLLM context.
    context = "\n\n---\n\n".join(
        f"### {c['section']}\n{c['content'].strip()[:1200]}" for c in chunks
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
            max_tokens  = 400,
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
    mode: str = "vector"   # "vector" | "pageindex"


# ── PageIndex helpers ──────────────────────────────────────────────────────────

async def _tree_search(query: str, nodes: list[dict]) -> tuple[list[str], dict]:
    """
    Three-level tree search — stays well within the 2048-token context window.

    L0 (~11 chapter titles)  → LLM picks 1-2 chapters   (~40 tokens out)
    L1 (~10 section titles per chapter) → LLM picks 1-3 sections (~60 tokens out)
    L2  collect ### children of chosen sections → return for generation

    Returns (node_ids, trace) where trace = {"chapters": [...], "sections": [...]}.
    """
    id_to_pos = {n["node_id"]: i for i, n in enumerate(nodes)}

    # ── L0: pick chapter(s) ───────────────────────────────────────────────────
    all_chapter_nodes = [n for n in nodes if n["level"] == 0]
    if not all_chapter_nodes:
        return [], {}

    # Only expose chapters that actually contain L1 sections to the LLM
    # (navigation-only headers like "ARIA — Navigation Index" have no L1 sections).
    # The filter strictly checks for level-1 nodes WITHIN the chapter boundary
    # (before the next level-0 node), not just anywhere in the next 5 positions.
    def _has_own_l1(cn: dict) -> bool:
        pos = id_to_pos.get(cn["node_id"], -1)
        if pos < 0:
            return False
        for n in nodes[pos + 1:]:
            if n["level"] == 0:
                return False   # hit next chapter without finding L1
            if n["level"] == 1:
                return True    # found L1 within this chapter
        return False

    chapter_nodes = [cn for cn in all_chapter_nodes if _has_own_l1(cn)] \
                    or all_chapter_nodes  # fallback to all if filter is too aggressive

    # Build enriched TOC: chapter title + up to 4 section-title hints per chapter
    # so the LLM has real context for informal / oblique queries.
    def _clean_nav_hint(nav_content: str, max_chars: int = 100) -> str:
        """Extract clean keyword text from nav_content, stripping markdown bold markers."""
        text = re.sub(r'\*\*[^*]+\*\*\s*', '', nav_content)   # strip **label** headers
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]

    toc_lines: list[str] = []
    for cn in chapter_nodes:
        toc_lines.append(f"[{cn['node_id']}] {cn['title']}")
        pos   = id_to_pos.get(cn["node_id"], -1)
        count = 0
        if pos >= 0:
            for child in nodes[pos + 1:]:
                if child["level"] == 0:
                    break
                if child["level"] == 1:
                    nav_hint = _clean_nav_hint(child.get("nav_content") or "", max_chars=60)
                    hint_str = f" [{nav_hint}]" if nav_hint else ""
                    toc_lines.append(f"    • {child['title']}{hint_str}")
                    count += 1
                    if count >= 5:
                        toc_lines.append("    …")
                        break
    toc_l0 = "\n".join(toc_lines)

    prompt_l0 = (
        "You are navigating ARIA technical documentation to answer a question.\n\n"
        "Chapters (each shows its key topics in [ ]):\n\n"
        f"{toc_l0}\n\n"
        f"Question: {query}\n\n"
        "Task: Look at the [ ] topic previews above. "
        "Pick the 1-2 chapter IDs that best cover the question's key topics. "
        "You MUST always pick at least 1 chapter. Output ONLY a JSON object on the first line, "
        "no explanation before or after it.\n"
        'Example output: {"node_ids": ["2"]}'
    )
    chapter_ids: list[str] = []
    try:
        resp = await _llm.chat.completions.create(
            model       = VLLM_MODEL,
            messages    = [{"role": "user", "content": prompt_l0}],
            max_tokens  = 80,
            temperature = 0.0,
            stream      = False,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
        m   = re.search(r'\{.*?\}', raw, re.DOTALL)
        if m:
            raw_ids = json.loads(m.group()).get("node_ids", [])
            valid   = {n["node_id"] for n in chapter_nodes}
            chapter_ids = [str(cid) for cid in raw_ids if str(cid) in valid]
        # Last-resort: extract any bare numbers from the response
        if not chapter_ids:
            valid = {n["node_id"] for n in chapter_nodes}
            found = re.findall(r'\b(\d+)\b', raw)
            chapter_ids = [f for f in found if f in valid][:2]
    except Exception as exc:
        log.warning("docs_tree_search_l0_failed", error=str(exc))

    # ── L0 keyword scoring (nav_content + titles) ─────────────────────────────
    # Used as fallback (when LLM fails) and as targeted correction for clear misroutes.
    # Uses MAX section score per chapter to prevent large chapters from dominating.
    _stop_words = {"the","a","an","is","are","was","were","be","do","did","does",
                   "how","why","what","who","which","where","when","have","has",
                   "in","on","of","for","to","and","or","not","it","its","i",
                   "you","we","they","this","that","with","over","just","than",
                   "vs","instead"}
    query_words = {w for w in re.sub(r"[^a-z0-9 ]", " ", query.lower()).split()
                   if w not in _stop_words and len(w) > 2}

    def _kw_score_chapter_max(cn: dict) -> int:
        """Max keyword-match score across any single L1 section in this chapter.
        Only matches against the **Keywords:** field (not Summary), so that
        chapters don't get inflated scores from incidental summary mentions."""
        pos = id_to_pos.get(cn["node_id"], -1)
        best = 0
        if pos < 0:
            return 0
        for child in nodes[pos + 1:]:
            if child["level"] == 0:
                break
            if child["level"] == 1:
                nav = child.get("nav_content") or ""
                # Extract only the Keywords field (stop before **Summary:**)
                kw_part = re.split(r'\*\*Summary:?\*\*', nav, maxsplit=1)[0]
                # Strip bold labels, then combine with section title
                kw_clean = re.sub(r'\*\*[^*]+\*\*\s*', '', kw_part).lower()
                text = kw_clean + " " + child.get("title","").lower()
                sec_score = sum(1 for w in query_words
                                if re.search(r'(?<![a-z0-9])' + re.escape(w) + r'(?![a-z0-9])', text))
                best = max(best, sec_score)
        return best

    kw_scored: list[tuple[int, str]] = sorted(
        [(_kw_score_chapter_max(cn), cn["node_id"]) for cn in chapter_nodes],
        reverse=True,
    )

    # Fallback: keyword match when LLM returned nothing
    if not chapter_ids:
        log.warning("docs_tree_search_l0_fallback", query=query)
        chapter_ids = [cid for _, cid in kw_scored[:2]] if kw_scored[0][0] > 0 else \
                      [n["node_id"] for n in chapter_nodes[:2]]
    else:
        # Targeted correction: only fire when the LLM's best chapter has very few
        # keyword matches AND the keyword winner clearly dominates (covers ≥ half the
        # query words and is ≥ 3 more than the LLM's best).  MAX scoring prevents
        # large chapters from dominating via quantity of sections.
        top_kw_score  = kw_scored[0][0] if kw_scored else 0
        llm_kw_scores = [s for s, cid in kw_scored if cid in chapter_ids]
        llm_best      = max(llm_kw_scores, default=0)
        half_query    = max(2, len(query_words) // 2)
        if top_kw_score >= half_query and (top_kw_score - llm_best) >= 2:
            kw_winner = kw_scored[0][1]
            if kw_winner not in chapter_ids:
                log.info("docs_tree_search_l0_kw_correction",
                         llm_ids=chapter_ids, kw_winner=kw_winner,
                         llm_score=llm_best, kw_score=top_kw_score)
                chapter_ids = [kw_winner] + chapter_ids[:1]

    chapter_titles = [n["title"] for n in chapter_nodes if n["node_id"] in chapter_ids]
    log.info("docs_tree_search_l0", chapter_ids=chapter_ids)

    # ── Collect L1 sections inside chosen chapters ─────────────────────────────
    section_nodes: list[dict] = []
    for cid in chapter_ids:
        if cid not in id_to_pos:
            continue
        pos = id_to_pos[cid]
        for n in nodes[pos + 1:]:
            if n["level"] == 0:
                break
            if n["level"] == 1:
                section_nodes.append(n)

    if not section_nodes:
        # Picked chapters have no L1 sections — broaden to all chapters
        log.warning("docs_tree_search_no_sections", chapter_ids=chapter_ids)
        for cn in chapter_nodes:
            if cn["node_id"] in chapter_ids:
                continue
            pos = id_to_pos.get(cn["node_id"], -1)
            if pos < 0:
                continue
            for n in nodes[pos + 1:]:
                if n["level"] == 0:
                    break
                if n["level"] == 1:
                    section_nodes.append(n)
            if len(section_nodes) >= 8:
                break
    if not section_nodes:
        return [], {}

    # ── L1: pick section(s) ───────────────────────────────────────────────────
    # Include a short content snippet so the LLM can distinguish similar titles
    toc_l1_lines: list[str] = []
    for n in section_nodes:
        # Use nav_content (keywords + summary) for routing — much richer than doc snippet
        nav = n.get("nav_content") or n.get("content", "")
        snippet = nav[:120].strip().replace("\n", " ")
        hint    = f" — {snippet}" if snippet else ""
        toc_l1_lines.append(f"[{n['node_id']}] {n['title'][:70]}{hint}")
    toc_l1 = "\n".join(toc_l1_lines)

    prompt_l1 = (
        "You are navigating ARIA technical documentation to answer a question.\n\n"
        "Sections (with content preview):\n"
        f"{toc_l1}\n\n"
        f"Question: {query}\n\n"
        "Task: Pick the 1-3 section IDs that best answer the question. "
        "You MUST always pick at least 1 section. Output ONLY a JSON object on the first line, "
        "no explanation before or after it.\n"
        'Example output: {"node_ids": ["5"]}'
    )
    section_ids: list[str] = []
    try:
        resp = await _llm.chat.completions.create(
            model       = VLLM_MODEL,
            messages    = [{"role": "user", "content": prompt_l1}],
            max_tokens  = 80,
            temperature = 0.0,
            stream      = False,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
        m   = re.search(r'\{.*?\}', raw, re.DOTALL)
        if m:
            parsed     = json.loads(m.group()).get("node_ids", [])
            valid_sids = {n["node_id"] for n in section_nodes}
            section_ids = [str(sid) for sid in parsed if str(sid) in valid_sids]
        # Last-resort: extract bare numbers from response
        if not section_ids:
            valid_sids = {n["node_id"] for n in section_nodes}
            found = re.findall(r'\b(\d+)\b', raw)
            section_ids = [f for f in found if f in valid_sids][:3]
    except Exception as exc:
        log.warning("docs_tree_search_l1_failed", error=str(exc))

    if not section_ids:
        # Fallback: keyword score sections, pick top 4
        query_words = set(query.lower().split())
        scored_s = sorted(
            section_nodes,
            key=lambda n: sum(1 for w in query_words if w in (n["title"] + " " + n.get("content", "")).lower()),
            reverse=True,
        )
        section_ids = [n["node_id"] for n in scored_s[:4]]
    section_titles = [n["title"] for n in section_nodes if n["node_id"] in section_ids]

    # ── Build nested tree for trace visualization ──────────────────────────────
    sid_set    = set(section_ids)
    trace_tree = []
    for cid in chapter_ids:
        if cid not in id_to_pos:
            continue
        cnode = next((n for n in chapter_nodes if n["node_id"] == cid), None)
        if not cnode:
            continue
        pos     = id_to_pos[cid]
        picked  = []
        for n in nodes[pos + 1:]:
            if n["level"] == 0:
                break
            if n["level"] == 1 and n["node_id"] in sid_set:
                picked.append(n["title"])
        trace_tree.append({"chapter": cnode["title"], "sections": picked})

    # ── L2: collect chosen sections + their ### children ──────────────────────
    result_ids: list[str] = []
    for sid in section_ids:
        if sid not in id_to_pos:
            continue
        pos = id_to_pos[sid]
        result_ids.append(sid)
        for n in nodes[pos + 1:]:
            if n["level"] <= 1:
                break
            result_ids.append(n["node_id"])

    trace = {"tree": trace_tree, "chapters": chapter_titles, "sections": section_titles}
    log.info("docs_tree_search", chapter_ids=chapter_ids, section_ids=section_ids,
             total=len(result_ids))
    return result_ids, trace


async def _pageindex_response(message: str) -> StreamingResponse:
    if not os.path.exists(INDEX_PATH):
        raise HTTPException(
            status_code=503,
            detail="PageIndex not built yet — call POST /docs-chat/build-index first.",
        )
    with open(INDEX_PATH, encoding="utf-8") as f:
        nodes = json.load(f)

    node_ids, _trace = await _tree_search(message, nodes)
    node_map = {n["node_id"]: n for n in nodes}

    # Pick up to 3 child nodes most relevant — use all children, cap total chars
    # to keep generation prompt under budget
    all_chunks = [
        {"section": node_map[nid]["title"], "content": node_map[nid]["content"]}
        for nid in node_ids if nid in node_map
    ]

    # Select up to 5 chunks (matches _build_prompt 1200-char-per-chunk budget)
    chunks: list[dict] = []
    total_chars = 0
    for c in all_chunks:
        if len(chunks) >= 5:
            break
        total_chars += len(c["content"])
        chunks.append(c)
        if total_chars >= 4000:
            break

    if not chunks:
        log.warning("docs_pageindex_no_match", query=message)
        chunks = [{"section": n["title"], "content": n["content"]} for n in nodes[:3]]

    log.info("docs_pageindex_search", matched=len(chunks), node_ids=node_ids[:5])
    prompt = _build_prompt(message, chunks)
    return StreamingResponse(_stream_answer(prompt), media_type="text/plain; charset=utf-8")


# ── SSE helpers (chat-v2) ─────────────────────────────────────────────────────

def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _sse_stream_answer(prompt: str):
    """Yield SSE token events from vLLM stream, then a done event."""
    try:
        stream = await _llm.chat.completions.create(
            model       = VLLM_MODEL,
            messages    = [{"role": "user", "content": prompt}],
            max_tokens  = 700,
            temperature = 0.3,
            stream      = True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield _sse({"type": "token", "content": delta})
    except Exception as exc:
        log.warning("docs_chat_llm_error_sse", error=str(exc))
        yield _sse({"type": "token", "content": f"\n[Error: {exc}]"})
    yield _sse({"type": "done"})


async def _sse_vector_response(message: str):
    """SSE generator for vector RAG: emit sources event then token stream."""
    embedding, _ = await embedding_client.get_embedding(message, is_query=True)
    if embedding is None:
        yield _sse({"type": "error", "detail": "Embedding service unavailable"})
        return

    vec_str = embedding_client.vec_to_pgvector_str(embedding)
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
        yield _sse({"type": "error", "detail": "Docs not ingested yet — call POST /docs-chat/ingest first."})
        return

    chunks = [{"section": r["section"], "content": r["content"]} for r in rows]
    yield _sse({
        "type": "sources",
        "sources": [
            {
                "section":    r["section"],
                "similarity": round(float(r["similarity"]), 3),
                "excerpt":    r["content"].strip(),
            }
            for r in rows
        ],
    })

    prompt = _build_prompt(message, chunks)
    async for event in _sse_stream_answer(prompt):
        yield event


async def _sse_pageindex_response(message: str):
    """SSE generator for PageIndex RAG: emit trace + sources events then token stream."""
    if not os.path.exists(INDEX_PATH):
        yield _sse({"type": "error", "detail": "PageIndex not built yet — call POST /docs-chat/build-index first."})
        return

    with open(INDEX_PATH, encoding="utf-8") as f:
        nodes = json.load(f)

    node_ids, trace = await _tree_search(message, nodes)
    node_map = {n["node_id"]: n for n in nodes}

    yield _sse({
        "type":     "trace",
        "tree":     trace.get("tree", []),
        "chapters": trace.get("chapters", []),
        "sections": trace.get("sections", []),
    })

    all_chunks = [
        {"section": node_map[nid]["title"], "content": node_map[nid]["content"]}
        for nid in node_ids if nid in node_map
    ]
    # Cap at 5 chunks (matches _build_prompt 1200-char-per-chunk budget)
    chunks: list[dict] = []
    total_chars = 0
    for c in all_chunks:
        if len(chunks) >= 5:
            break
        total_chars += len(c["content"])
        chunks.append(c)
        if total_chars >= 4000:
            break

    if not chunks:
        log.warning("docs_pageindex_no_match_sse", query=message)
        chunks = [{"section": n["title"], "content": n["content"]} for n in nodes[:3]]

    yield _sse({
        "type": "sources",
        "sources": [
            {"section": c["section"], "excerpt": c["content"].strip()}
            for c in chunks
        ],
    })

    log.info("docs_pageindex_search_sse", matched=len(chunks), node_ids=node_ids[:5])
    prompt = _build_prompt(message, chunks)
    async for event in _sse_stream_answer(prompt):
        yield event


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/chat")
async def docs_chat(body: ChatRequest, _=Depends(require_api_key)):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message required")
    if len(body.message) > 500:
        raise HTTPException(status_code=400, detail="message too long")
    if body.mode not in ("vector", "pageindex"):
        raise HTTPException(status_code=400, detail="mode must be 'vector' or 'pageindex'")

    if body.mode == "pageindex":
        return await _pageindex_response(body.message)

    # ── Vector RAG path ────────────────────────────────────────────────────────
    # Embed query with Ollama nomic-embed-text
    embedding, _ = await embedding_client.get_embedding(body.message, is_query=True)
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


async def docs_build_index() -> dict:
    """
    Build the PageIndex tree using a two-file architecture:
      ARIA_NAV.md  — navigation layer: keywords + summaries optimised for routing
      ARIA_DOCS.md — content layer:    full technical content for answer generation

    Each index node uses NAV content for LLM tree navigation and DOC content
    for the answer prompt. ARIA_DOCS.md is never modified.
    Safe to re-run. Called on startup and via the HTTP endpoint below.
    """
    if not os.path.exists(DOCS_PATH):
        raise FileNotFoundError(f"Docs not found at {DOCS_PATH}")

    # ── Build doc content map: section_title → full technical content ──────────
    doc_text   = open(DOCS_PATH, encoding="utf-8").read()
    doc_chunks = _parse_chunks(doc_text)
    doc_map    = {c["section"]: c["content"] for c in doc_chunks}

    # ── Parse nav file (if available) or fall back to doc file ─────────────────
    if os.path.exists(NAV_PATH):
        nav_text   = open(NAV_PATH, encoding="utf-8").read()
        nav_chunks = _parse_chunks(nav_text)
        log.info("docs_index_using_nav", nav_path=NAV_PATH, nav_sections=len(nav_chunks))
    else:
        nav_chunks = doc_chunks
        log.warning("docs_index_no_nav_file", fallback="using ARIA_DOCS.md for navigation")

    # ── Build combined nodes ───────────────────────────────────────────────────
    nodes = []
    for i, chunk in enumerate(nav_chunks, start=1):
        title       = chunk["section"]
        # nav_content: keywords + summary from ARIA_NAV.md (used for LLM routing)
        nav_content = chunk["content"]
        # content: full technical text from ARIA_DOCS.md (used for answer generation)
        doc_content = doc_map.get(title, "")
        # If doc has no entry for this title, fall back to nav content
        content     = doc_content if doc_content else nav_content
        nodes.append({
            "node_id":     str(i),
            "title":       title,
            "level":       chunk["level"],
            "nav_content": nav_content,
            "content":     content,
        })

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(nodes, f, ensure_ascii=False, indent=2)

    matched = sum(1 for n in nodes if n["content"] != n["nav_content"])
    log.info("docs_index_built", total=len(nodes), doc_matched=matched, path=INDEX_PATH)
    return {"status": "ok", "total": len(nodes), "doc_matched": matched, "path": INDEX_PATH}


@router.post("/build-index")
async def docs_build_index_endpoint(_=Depends(require_api_key)):
    """HTTP endpoint: build PageIndex tree and save to disk."""
    try:
        return await docs_build_index()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


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
            if chunk["level"] == 0:
                continue   # chapter nodes are navigation-only, nothing to embed
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


@router.post("/chat-v2")
async def docs_chat_v2(body: ChatRequest, _=Depends(require_api_key)):
    """
    SSE endpoint — streams structured events for the split-panel UI.

    Events (newline-delimited):
      data: {"type":"sources","sources":[{"section","similarity?","excerpt"},...]}
      data: {"type":"trace","chapters":[...],"sections":[...]}   (pageindex only)
      data: {"type":"token","content":"..."}                     (repeated)
      data: {"type":"done"}
      data: {"type":"error","detail":"..."}                      (on failure)
    """
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message required")
    if len(body.message) > 500:
        raise HTTPException(status_code=400, detail="message too long")
    if body.mode not in ("vector", "pageindex"):
        raise HTTPException(status_code=400, detail="mode must be 'vector' or 'pageindex'")

    gen = (
        _sse_pageindex_response(body.message)
        if body.mode == "pageindex"
        else _sse_vector_response(body.message)
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
