"""Set dummy env BEFORE importing the backend modules (agent.py raises when a key is missing).

MCP_DB_PATH points to a temp dir in the MCP server tests, so they never touch a real database.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("AGENTBASE_MEMORY_ID", "memory-test")
os.environ.setdefault("MEMORY_STRATEGY_ID", "ltms-cust-test")
os.environ.setdefault("MCP_RESTAURANT_URL", "https://gw.example/restaurant")

BACKEND = Path(__file__).resolve().parent.parent / "src" / "backend"
MCP_SERVER = Path(__file__).resolve().parent.parent / "src" / "mcp-server"
# BACKEND must come FIRST on sys.path: both folders have a main.py, and
# `import main` in a test has to resolve to src/backend/main.py.
sys.path.insert(0, str(MCP_SERVER))
sys.path.insert(0, str(BACKEND))
