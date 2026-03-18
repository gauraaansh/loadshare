"""
ARIA PageIndex Builder
========================
Triggers the MCP server's /docs-chat/build-index endpoint which:
  1. Parses ARIA_DOCS.md into sections (split on ## / ### headers)
  2. Assigns sequential node IDs
  3. Saves the tree to /tmp/aria_docs_index.json (no embeddings, no LLM calls)

Run after `docker compose up -d` and all services are healthy:
    python scripts/build_docs_index.py

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

print(f"Building PageIndex at {MCP_HOST}/docs-chat/build-index ...")

try:
    resp = requests.post(
        f"{MCP_HOST}/docs-chat/build-index",
        headers={"X-API-Key": MCP_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"Done — {data['total']} sections indexed.")
except requests.exceptions.ConnectionError:
    print("ERROR: Could not connect to MCP server. Is `docker compose up` running?")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)
