"""
ARIA — MCP Server: Configuration
===================================
All constants and env-var-driven settings in one place.
Import from here everywhere — no os.getenv() scattered across files.
"""

import os

# ── Service ────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://aria:aria_secret@localhost:5432/aria_db")
REDIS_URL    = os.getenv("REDIS_URL",    "redis://localhost:6379/0")
PORT         = int(os.getenv("MCP_PORT", "8001"))

# ── Auth ───────────────────────────────────────────────────────
MCP_API_KEY     = os.getenv("MCP_API_KEY",     "aria_mcp_key_change_me")
ML_INTERNAL_KEY = os.getenv("ML_INTERNAL_KEY", "aria-ml-internal-dev-key")

# ── External services ──────────────────────────────────────────
VLLM_HOST         = os.getenv("VLLM_HOST",         "http://vllm:8000")
ML_HOST           = os.getenv("ML_HOST",           "http://fastapi-ml:8002")
OLLAMA_HOST       = os.getenv("OLLAMA_HOST",       "http://ollama:11434")
EVENT_STREAM_HOST = os.getenv("EVENT_STREAM_HOST", "http://event-stream:8003")

# ── LangSmith observability ───────────────────────────────────
LANGCHAIN_TRACING_V2 = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
LANGCHAIN_API_KEY    = os.getenv("LANGCHAIN_API_KEY", "")
LANGCHAIN_PROJECT    = os.getenv("LANGCHAIN_PROJECT", "aria")

# ── Cycle cadence ──────────────────────────────────────────────
# Same env var as event-stream — both services read CYCLE_INTERVAL_MINS.
# Change once in .env, both update.
CYCLE_INTERVAL_MINS = int(os.getenv("CYCLE_INTERVAL_MINS", "15"))

# ── Risk thresholds ────────────────────────────────────────────
DEAD_ZONE_RISK_THRESHOLD    = float(os.getenv("DEAD_ZONE_RISK_THRESHOLD",    "0.60"))
RESTAURANT_RISK_THRESHOLD   = float(os.getenv("RESTAURANT_RISK_THRESHOLD",   "0.65"))
EPH_TARGET_SUPPLEMENTARY    = float(os.getenv("EPH_TARGET_SUPPLEMENTARY",    "90.0"))
EPH_TARGET_DEDICATED        = float(os.getenv("EPH_TARGET_DEDICATED",        "100.0"))
HEALTH_SCORE_THRESHOLD      = float(os.getenv("HEALTH_SCORE_THRESHOLD",      "40.0"))
CHURN_SIGNAL_SESSIONS       = int(os.getenv("CHURN_SIGNAL_SESSIONS",         "3"))
DEAD_ZONE_DENSITY_THRESHOLD = float(os.getenv("DEAD_ZONE_DENSITY_THRESHOLD", "0.3"))

# ── RAG / Episodic Memory (Phase 2) ────────────────────────────
OLLAMA_EMBEDDING_MODEL   = os.getenv("OLLAMA_EMBEDDING_MODEL",  "qwen3-embedding")
RAG_SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.65"))
RAG_RECENCY_DAYS         = int(os.getenv("RAG_RECENCY_DAYS",    "30"))
RAG_MIN_SUPPORT          = int(os.getenv("RAG_MIN_SUPPORT",     "2"))    # min grounded episodes before RAG activates
RAG_TOP_K                = int(os.getenv("RAG_TOP_K",           "3"))    # max snippets injected into LLM prompt
RAG_SNIPPET_MAX_CHARS    = int(os.getenv("RAG_SNIPPET_MAX_CHARS", "1200"))  # total budget for all snippets
