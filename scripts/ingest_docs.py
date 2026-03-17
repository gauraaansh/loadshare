"""
ARIA Docs Ingest Script
========================
Triggers the MCP server's /docs-chat/ingest endpoint which:
  1. Parses ARIA_DOCS.md into chunks (split on ## / ### headers)
  2. Embeds each chunk with Ollama nomic-embed-text (768-dim)
  3. Stores all chunks in the docs_chunks pgvector table

Run after `docker compose up -d` and all services are healthy:
    python scripts/ingest_docs.py

Optional env overrides:
    MCP_HOST=http://localhost:8001  MCP_API_KEY=aria_mcp_key_change_me
"""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

MCP_HOST = os.getenv("MCP_HOST", "http://localhost:8001")
MCP_KEY  = os.getenv("MCP_API_KEY", "aria_mcp_key_change_me")

print(f"Triggering ingest at {MCP_HOST}/docs-chat/ingest ...")

try:
    resp = requests.post(
        f"{MCP_HOST}/docs-chat/ingest",
        headers={"X-API-Key": MCP_KEY},
        timeout=300,   # embedding 172 chunks takes ~60-90s
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"Done — {data['inserted']}/{data['total']} chunks ingested.")
except requests.exceptions.ConnectionError:
    print("ERROR: Could not connect to MCP server. Is `docker compose up` running?")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)
